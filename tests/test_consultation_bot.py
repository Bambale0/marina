"""Telegram consultation checkout through public APIs and a fake external transport."""

import json

import pytest

from marina.bot import Bot, handle_update
from marina.db import connect
from tests.test_consultation_commerce import app as consultation_app, confirmed_consultation
from tests.test_funnel import drain


@pytest.fixture
def app(tmp_path):
    return consultation_app.__wrapped__(tmp_path)


def transport_capture(app):
    calls = []

    def transport(method, url, data, headers):
        calls.append((method, url.rsplit("/", 1)[-1], data))
        if url == "https://gate.lava.top/api/v3/invoice":
            return {"id": "consultation-fixture", "paymentUrl": "https://app.lava.top/consultation"}
        return {"ok": True, "result": {"message_id": 91}}

    app.config["HTTP_TRANSPORT"] = transport
    return calls


def messages(calls):
    return [data for _, endpoint, data in calls if endpoint == "sendMessage"]


def click(app, button, update_id):
    handle_update(
        app,
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": 2, "first_name": "Buyer"},
                "data": button["callback_data"],
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )


def send_email(app, text, update_id):
    handle_update(
        app,
        {
            "update_id": update_id,
            "message": {
                "from": {"id": 2, "first_name": "Buyer"},
                "chat": {"id": 2, "type": "private"},
                "text": text,
            },
        },
    )


def payment_button(calls):
    return next(
        button
        for message in reversed(messages(calls))
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
        if "консультаци" in button["text"].lower()
        and ("оплатить" in button["text"].lower() or "без оплаты" in button["text"].lower())
    )


def show_proposal(app, admin, booking, calls):
    proposal = admin.post(
        f"/api/admin/bookings/{booking['id']}/propose",
        json={
            "revision": booking["revision"],
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
            "reason": "Synthetic consultation time",
        },
    )
    assert proposal.status_code == 200, proposal.json
    Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "proposal-card").card(booking["id"], "booking")
    confirm = messages(calls)[-1]["reply_markup"]["inline_keyboard"][0][0]
    click(app, confirm, 9100)


@pytest.mark.parametrize("paid_pdf,total", [(True, 390000), (False, 600000)])
def test_confirmed_consultation_gets_server_price_email_and_lava_link(app, paid_pdf, total):
    admin, owner, run, booking, _, _, _, _ = confirmed_consultation(
        app, paid_pdf=paid_pdf, confirm=False
    )
    calls = transport_capture(app)
    show_proposal(app, admin, booking, calls)
    button = payment_button(calls)
    assert f"{total // 100},00 ₽" in button["text"]
    offer_text = messages(calls)[-1]["text"]
    assert f"К оплате: {total // 100},00 ₽" in offer_text
    if paid_pdf:
        assert "Скидка по промокоду" in offer_text
        assert "Synthetic reserved PDF coupon terms." in offer_text
    else:
        assert "полная стоимость" in offer_text
        assert "Synthetic reserved PDF coupon terms." not in offer_text
    assert "Synthetic purchase" not in offer_text
    click(app, button, 9101)
    assert "email" in messages(calls)[-1]["text"]
    with connect(app.config["DATABASE"]) as db:
        orders = [
            json.loads(row[0])
            for row in db.execute("SELECT data FROM objects WHERE kind='order'")
            if json.loads(row[0]).get("consultation_run_id")
        ]
    assert len(orders) == 1
    assert orders[0]["consultation_run_id"] == run["id"] and "run_id" not in orders[0]
    assert orders[0]["total"] == total
    click(app, button, 9102)
    send_email(app, "invalid-email", 9103)
    assert "email" in messages(calls)[-1]["text"]
    send_email(app, "buyer@example.test", 9104)
    drain(app)
    assert any(
        button.get("url") == "https://app.lava.top/consultation"
        for message in messages(calls)
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
    )
    invoices = [data for _, endpoint, data in calls if endpoint == "invoice"]
    assert len(invoices) == 1 and invoices[0]["amount"] == total / 100
    assert invoices[0]["offerId"] == "fixture-consultation-offer"
    assert not any(endpoint in ("sendInvoice", "createInvoiceLink") for _, endpoint, _ in calls)
    assert not owner.get(f"/api/bookings/{booking['id']}").json["data"].get("paid_at")
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM objects WHERE kind='order' "
                "AND json_extract(data,'$.consultation_run_id')=?",
                (run["id"],),
            ).fetchone()[0]
            == 1
        )


def test_consultation_payment_is_resumable_from_history_and_refreshes_expired_quote(app):
    _, _, _, booking, _, _, _, _ = confirmed_consultation(app)
    calls = transport_capture(app)
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "confirmed-card")
    bot.card(booking["id"], "booking")
    old = payment_button(calls)
    app.clock[0] += 901
    click(app, old, 9200)
    assert "обновлён" in messages(calls)[-1]["text"].lower()
    fresh = payment_button(calls)
    click(app, fresh, 9201)
    assert "email" in messages(calls)[-1]["text"]


def test_admin_consultation_access_never_asks_for_email_or_creates_invoice(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    app.config["ADMIN_TELEGRAM_IDS"] = {1, 2}
    calls = transport_capture(app)
    bot = Bot(app, {"id": 2, "first_name": "Admin"}, 2, "admin-card")
    bot.card(booking["id"], "booking")
    button = payment_button(calls)
    click(app, button, 9300)
    assert "без оплаты" in messages(calls)[-1]["text"].lower()
    assert not any("email" in message["text"] for message in messages(calls))
    assert not any(endpoint == "invoice" for _, endpoint, _ in calls)
    assert owner.get(f"/api/bookings/{booking['id']}").json["data"]["paid_at"] == app.clock[0]
    click(app, button, 9301)
    assert "без оплаты" in messages(calls)[-1]["text"].lower()


def test_unconfigured_payment_keeps_confirmation_and_reserved_price(app):
    admin, owner, _, booking, tariff, _, _, _ = confirmed_consultation(app, confirm=False)
    archived = admin.post(
        f"/api/admin/content/{tariff['id']}/archive",
        json={"revision": tariff["revision"], "reason": "Consultation offer not ready"},
    )
    assert archived.status_code == 200, archived.json
    calls = transport_capture(app)
    show_proposal(app, admin, booking, calls)
    assert owner.get(f"/api/bookings/{booking['id']}").json["status"] == "confirmed"
    text = "\n".join(message["text"] for message in messages(calls))
    assert "Время подтверждено" in text
    assert "3900,00 ₽" in text
    assert "Оплата консультации пока недоступна" in text
    assert not any(
        "Оплатить консультацию" in button["text"]
        for message in messages(calls)
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
    )
    assert not any(endpoint == "invoice" for _, endpoint, _ in calls)
