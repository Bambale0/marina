"""Source-contract regressions. Synthetic SQLite and mocked external boundaries only."""

from datetime import datetime, timezone

import pytest

from marina.bot import Bot
from marina.db import connect, transaction, unpack, update
from tests.test_identity import app, login
from tests.test_funnel import (
    private_source, setup_sale, scenario, start, finish, capture, drain, approve_existing,
)
from tests.test_commerce import pay
from tests.test_screening import publish


def consent(admin, owner):
    policy = publish(admin, "policy", {
        "type": "privacy", "terms": "Synthetic consent for regression",
        "media_max_bytes": 10000, "retention_days": 30,
    })
    assert owner.post("/api/consents", json={"policy_id": policy["id"], "accepted": True}).status_code == 200


def test_resumed_red_result_retains_diagnostic_actions(app):
    admin, owner = login(app), login(app, 2)
    consent(admin, owner)
    scenario(admin)
    sent, _ = capture(app)
    run = finish(owner, start(owner), ["c"] * 6)
    bot = Bot(app, {"id": 2, "first_name": "Parent"}, 2, "resume-red")
    bot.state_set(funnel_run_id=run["id"])
    bot.action("funnel_resume", {})
    labels = [b["text"] for row in sent[-1][1].get("reply_markup", {}).get("inline_keyboard", []) for b in row]
    assert labels == ["📋 Что входит в диагностику", "📅 Записаться на диагностику"]


def test_cart_payment_help_opens_specialist_contact(app):
    _, owner, run, config, _, sent, _ = setup_sale(app)
    owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    app.clock[0] += 1800
    drain(app)
    buttons = [b for row in sent[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    support = next(b for b in buttons if "Помощь" in b["text"])
    assert support.get("url") == config["data"]["contact_url"]
    assert "callback_data" not in support


def test_paid_confirmation_precedes_pdf_and_is_not_repeated_on_file_retry(app):
    _, owner, run, _, _, sent, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    transport = app.config["HTTP_TRANSPORT"]
    events, failed = [], []

    def send_message(method, url, data, headers):
        if "Оплата прошла успешно" in data.get("text", ""):
            events.append("confirmation")
        return transport(method, url, data, headers)

    def send_document(*args):
        events.append("document")
        if not failed:
            failed.append(True)
            raise RuntimeError("Synthetic upload failure")
        files.append(args)

    app.config["HTTP_TRANSPORT"] = send_message
    app.config["FILE_TRANSPORT"] = send_document
    assert pay(app, order).status_code == 200
    drain(app)
    assert events == ["confirmation", "document"]
    assert not files
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE object_id=? AND event LIKE 'funnel.followup.%'", (run["id"],)).fetchone()[0] == 0
    app.clock[0] += 60
    drain(app)
    assert events == ["confirmation", "document", "document"]
    assert len(files) == 1
    assert owner.get(f"/api/funnel-runs/{run['id']}").json["data"]["guide_delivered_at"]


def test_admin_free_pdf_does_not_claim_successful_payment(app):
    admin, _, _, _, _, sent, files = setup_sale(app)
    run = finish(admin, start(admin, key="admin-source"), ["b"] * 6)
    response = admin.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 200, response.json
    drain(app)
    assert len(files) == 1
    assert not any("Оплата прошла успешно" in d.get("text", "") for _, d in sent)
    assert any("без оплаты" in d.get("text", "") for _, d in sent)
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_old_progress_event_after_deploy_is_a_safe_noop(app):
    from marina.funnel import notify_funnel

    admin, owner = login(app), login(app, 2)
    scenario(admin)
    sent, _ = capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    account = owner.get("/api/me").json["account"]["id"]
    notify_funnel(app, account, run["id"], "funnel.progress.5")
    assert not sent


def test_previous_buyer_gets_access_instead_of_a_second_offer(app):
    _, owner, original, _, _, sent, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{original['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    again = finish(owner, start(owner, key="repeat-paid-guide"), ["b"] * 6)
    drain(app)
    app.clock[0] += 3
    drain(app)
    marker = len(sent)
    app.clock[0] += 180
    drain(app)
    assert not any("Забрать руководство за" in str(d) for _, d in sent[marker:])
    assert any("Открыть руководство" in str(d) for _, d in sent[marker:])
    repeated = owner.post(f"/api/funnel-runs/{again['id']}/purchase", json={})
    assert repeated.status_code == 200, repeated.json
    assert repeated.json["id"] == order["id"]
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='order'").fetchone()[0] == 1


def test_followup_uses_configured_booking_action_and_actual_price(app):
    _, owner, run, _, _, sent, _ = setup_sale(app)
    with transaction(app.config["DATABASE"]) as db:
        current = unpack(db.execute("SELECT * FROM objects WHERE id=?", (run["id"],)).fetchone())
        snapshot = current["data"]["snapshot"]
        snapshot["followups"][1].update(button_label="Записаться за {consultation_price}", button_action="book")
        update(db, current, app.clock[0], {**current["data"], "snapshot": snapshot})
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    app.clock[0] += 48 * 3600
    drain(app)
    button = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Записаться за 10 ₽"
    with connect(app.config["DATABASE"]) as db:
        import json
        data = json.loads(db.execute("SELECT data FROM objects WHERE id=?", (button["callback_data"][2:],)).fetchone()[0])
    assert data["action"] == "funnel_book"


def test_free_pdf_remains_available_after_material_superseded(app):
    admin, owner = login(app), login(app, 2)
    base = scenario(admin)
    material = admin.post(f"/api/admin/funnels/{base['id']}/prepare-guide", json={"revision": base["revision"], "reason": "Synthetic original"}).json
    material = approve_existing(admin, material)
    scenario(admin, free_material_id=material["id"])
    run = finish(owner, start(owner), ["a"] * 6)
    before = owner.get(f"/api/funnel-runs/{run['id']}/free-pdf")
    assert before.status_code == 200
    response = admin.post(f"/api/admin/content/{material['id']}/archive", json={"revision": material["revision"], "reason": "Superseded, existing access retained"})
    assert response.status_code == 200
    after = owner.get(f"/api/funnel-runs/{run['id']}/free-pdf")
    assert after.status_code == 200 and after.data == before.data


@pytest.mark.parametrize("verification", ["pending", "unknown"])
def test_lava_nonterminal_success_is_rechecked_without_another_charge(app, verification):
    app.config.update(DEV_EXTERNAL_LAVA_CHECKOUT=True, LAVA_API_KEY="fixture", LAVA_WEBHOOK_SECRET="fixture-webhook", PUBLIC_URL="https://fixture.test")
    _, owner, run, _, _, _, files = setup_sale(app, currency="RUB", lava_offer_id="fixture-offer", web_sales_approved=True)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    upstream = app.config["HTTP_TRANSPORT"]
    calls = []
    verified = [False]
    paid_at = datetime.fromtimestamp(app.clock[0], timezone.utc).isoformat()

    def transport(method, url, data, headers):
        if not url.startswith("https://gate.lava.top"):
            return upstream(method, url, data, headers)
        calls.append(method)
        if method == "POST":
            return {"id": "fixture-contract", "paymentUrl": "https://example.test/pay"}
        if not verified[0]:
            return {"id": "fixture-contract", "status": verification}
        return {"id": "fixture-contract", "status": "completed", "datetime": paid_at, "receipt": {"amount": 1.23, "currency": "RUB"}}

    app.config["HTTP_TRANSPORT"] = transport
    assert owner.post(f"/api/orders/{order['id']}/checkout", json={"email": "buyer@example.test"}).status_code == 202
    drain(app)
    webhook = {"eventType": "payment.success", "contractId": "fixture-contract", "amount": 1.23, "currency": "RUB", "timestamp": paid_at, "status": "completed"}
    client = app.test_client()
    assert client.post("/api/webhooks/lava", headers={"X-Api-Key": "fixture-webhook"}, json=webhook).status_code == 200
    drain(app)
    assert calls == ["POST", "GET", "GET"]
    assert not files
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='payment_event' AND status='processed'").fetchone()[0] == 0
    # The provider becomes consistent later. No second callback is required to recover.
    verified[0] = True
    app.clock[0] += 60
    drain(app)
    assert calls == ["POST", "GET", "GET", "GET"]
    assert len(files) == 1
    assert client.post("/api/webhooks/lava", headers={"X-Api-Key": "fixture-webhook"}, json=webhook).status_code == 200
    drain(app)
    assert calls == ["POST", "GET", "GET", "GET"]
    assert len(files) == 1
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM objects WHERE kind='right'").fetchone()[0] == 1


@pytest.mark.parametrize("queued", [False, True])
def test_refusal_blocks_new_or_unstarted_invoice_without_provider_calls(app, queued):
    _, owner, run, _, _, sent, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    if queued:
        assert owner.post(f"/api/orders/{order['id']}/checkout", json={}).status_code == 202
    assert owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={}).status_code == 200
    assert owner.post(f"/api/orders/{order['id']}/checkout", json={}).status_code == 422
    drain(app)
    assert not any(method == "createInvoiceLink" for method, _ in sent)
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='reconciliation'").fetchone()[0] == 0


def test_received_payment_after_refusal_is_not_discarded(app):
    _, owner, run, _, _, _, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={}).status_code == 200
    assert pay(app, order).status_code == 200
    drain(app)
    assert len(files) == 1
    assert pay(app, order).status_code == 200
    drain(app)
    assert len(files) == 1
