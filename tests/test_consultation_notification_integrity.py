"""Paid appointment cancellations and notification retries preserve financial facts."""

import json

import pytest

from marina import Problem
from marina.bot import handle_update
from marina.db import connect
from marina.operations import notify
from tests.test_commerce import pay
from tests.test_consultation_commerce import app, confirmed_consultation, login, quote


@pytest.fixture
def paid_consultation(app):
    admin, owner, _, booking, _, _, _, _ = confirmed_consultation(app, paid_pdf=False)
    calculation = quote(owner, booking)
    response = owner.post(
        "/api/orders", json={"quote_id": calculation.json["id"], "key": "paid-notice"}
    )
    assert response.status_code == 200, response.json
    order = response.json
    assert pay(app, order).status_code == 200
    return admin, owner, owner.get("/api/bookings/" + booking["id"]).json, order


def capture_notices(app):
    calls = []

    def transport(method, url, data, headers):
        calls.append(data)
        return {"ok": True, "result": {"message_id": len(calls)}}

    app.config["HTTP_TRANSPORT"] = transport
    return calls


@pytest.mark.parametrize("actor", ["client", "admin"])
def test_paid_cancellation_creates_manual_review_and_never_promises_a_meeting(
    app, paid_consultation, actor
):
    admin, owner, booking, order = paid_consultation
    client = owner if actor == "client" else admin
    route = "/api/bookings/" if actor == "client" else "/api/admin/bookings/"
    response = client.post(
        route + booking["id"] + "/cancel",
        json={
            "revision": booking["revision"],
            "reason": "Synthetic cancellation",
        },
    )
    assert response.status_code == 200, response.json
    saved = owner.get("/api/bookings/" + booking["id"]).json
    assert saved["status"] == "cancelled"
    assert saved["data"]["paid_at"] == booking["data"]["paid_at"]
    assert saved["data"]["order_id"] == order["id"]
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid"
    with connect(app.config["DATABASE"]) as db:
        reviews = [
            json.loads(row[0])
            for row in db.execute("SELECT data FROM objects WHERE kind='reconciliation'")
        ]
        assert len(reviews) == 1
        assert reviews[0]["issue"] == "paid_consultation_cancelled"
        assert reviews[0]["booking_id"] == booking["id"]
        assert reviews[0]["order_id"] == order["id"]
        assert (
            db.execute("SELECT status FROM receipts WHERE order_id=?", (order["id"],)).fetchone()[0]
            == "confirmed"
        )
        assert db.execute("SELECT count(*) FROM objects WHERE kind='refund'").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE event='consultation.cancelled'"
            ).fetchone()[0]
            == 1
        )
    calls = capture_notices(app)
    notify(app, booking["owner"], booking["id"], "consultation.paid")
    notify(app, booking["owner"], booking["id"], "consultation.cancelled")
    client_texts = [call["text"] for call in calls if call.get("chat_id") == 2]
    assert len(client_texts) == 1
    assert "отменена" in client_texts[0] and "возврат" in client_texts[0]
    assert "пришлёт ссылку" not in client_texts[0] and "Время:" not in client_texts[0]
    assert admin.get("/api/admin/queue").json["items"][0]["kind"] == "reconciliation"


def test_paid_specialist_notice_opens_the_confirmed_booking_directly(app, paid_consultation):
    _, _, booking, _ = paid_consultation
    calls = capture_notices(app)
    notify(app, booking["owner"], booking["id"], "consultation.paid")
    specialist = next(call for call in calls if call.get("chat_id") == 1)
    button = specialist["reply_markup"]["inline_keyboard"][0][0]
    handle_update(
        app,
        {
            "update_id": 99001,
            "callback_query": {
                "id": "paid-booking",
                "from": {"id": 1},
                "data": button["callback_data"],
                "message": {"chat": {"id": 1, "type": "private"}},
            },
        },
    )
    assert "booking · confirmed" in calls[-1]["text"]
    assert booking["id"] in calls[-1]["text"]


def test_paid_notice_retry_skips_every_acknowledged_recipient(app, paid_consultation):
    _, _, booking, _ = paid_consultation
    app.config["ADMIN_TELEGRAM_IDS"] = {1, 3}
    login(app, 3)
    calls = []
    failed = [False]

    def transport(method, url, data, headers):
        calls.append(data)
        if data.get("chat_id") == 3 and not failed[0]:
            failed[0] = True
            raise Problem("Synthetic specialist send failed", 503)
        return {"ok": True, "result": {"message_id": len(calls)}}

    app.config["HTTP_TRANSPORT"] = transport
    with pytest.raises(Problem):
        notify(app, booking["owner"], booking["id"], "consultation.paid")
    notify(app, booking["owner"], booking["id"], "consultation.paid")
    assert [call["chat_id"] for call in calls].count(2) == 1
    assert [call["chat_id"] for call in calls].count(1) == 1
    assert [call["chat_id"] for call in calls].count(3) == 2
    count = len(calls)
    notify(app, booking["owner"], booking["id"], "consultation.paid")
    assert len(calls) == count


def test_cancellation_stops_an_unstarted_invoice_without_a_provider_request(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app, paid_pdf=False)
    calculation = quote(owner, booking)
    order = owner.post(
        "/api/orders", json={"quote_id": calculation.json["id"], "key": "cancel-unpaid"}
    ).json
    attempt = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert attempt.status_code == 202, attempt.json
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail("Cancellation must not call Lava")
    response = owner.post("/api/bookings/" + booking["id"] + "/cancel", json={})
    assert response.status_code == 200, response.json
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT status FROM objects WHERE id=?", (attempt.json["id"],)).fetchone()[0]
            == "cancelled"
        )
        assert (
            db.execute(
                "SELECT status FROM outbox WHERE object_id=? AND event='invoice.create'",
                (attempt.json["id"],),
            ).fetchone()[0]
            == "cancelled"
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='reconciliation'").fetchone()[0]
            == 0
        )
