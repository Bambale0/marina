"""Release follow-through: public API, real temporary SQLite, external transport mocks."""

import json
from datetime import datetime, timezone

import pytest

from marina.bot import Bot
from marina.db import connect, insert, transaction, unpack, update
from tests.test_identity import app, login
from tests.test_funnel import private_source, setup_sale, finish, start, drain, capture
from tests.test_commerce import pay


def repeat_buyer(app):
    admin, owner, run, config, product, sent, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    again = finish(owner, start(owner, key="followthrough-repeat"), ["b"] * 6)
    app.clock[0] += 3
    drain(app)
    return admin, owner, again, product, sent


@pytest.mark.parametrize("sales", ["archived", "unconfigured"])
def test_existing_guide_is_offered_even_when_new_sales_are_unavailable(app, sales):
    _, owner, again, product, sent = repeat_buyer(app)
    with transaction(app.config["DATABASE"]) as db:
        if sales == "archived":
            current = unpack(
                db.execute("SELECT * FROM objects WHERE id=?", (product["id"],)).fetchone()
            )
            update(db, current, app.clock[0], status="archived")
        else:
            current = unpack(
                db.execute("SELECT * FROM objects WHERE id=?", (again["id"],)).fetchone()
            )
            current["data"]["snapshot"]["guide_product_id"] = None
            update(db, current, app.clock[0], current["data"])
    marker = len(sent)
    app.clock[0] += 200
    drain(app)
    assert any("Открыть руководство" in str(data) for _, data in sent[marker:])
    assert not any("Забрать руководство за" in str(data) for _, data in sent[marker:])


def test_revoked_repeat_entitlement_is_hidden_and_resume_restores_purchase(app):
    _, owner, again, _, sent = repeat_buyer(app)
    app.clock[0] += 200
    drain(app)
    saved = owner.get(f"/api/funnel-runs/{again['id']}").json
    right_id = saved["data"]["owned_guide_id"]
    with transaction(app.config["DATABASE"]) as db:
        right = unpack(db.execute("SELECT * FROM objects WHERE id=?", (right_id,)).fetchone())
        update(db, right, app.clock[0], status="revoked")
    fetched = owner.get(f"/api/funnel-runs/{again['id']}").json
    assert "funnel_owned" not in str(fetched["data"]["result_buttons"])
    marker = len(sent)
    assert owner.post(f"/api/funnel-runs/{again['id']}/resume", json={}).status_code == 200
    drain(app)
    assert any("Забрать руководство за" in str(data) for _, data in sent[marker:])
    purchased = owner.post(f"/api/funnel-runs/{again['id']}/purchase", json={})
    assert purchased.status_code == 200, purchased.json
    assert purchased.json["status"] == "pending"


def lava_sale(app):
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture-webhook",
        PUBLIC_URL="https://fixture.test",
    )
    _, owner, run, _, _, _, files = setup_sale(
        app, currency="RUB", lava_offer_id="fixture-offer", web_sales_approved=True
    )
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    original = app.config["HTTP_TRANSPORT"]
    state, calls = {"mode": "pending"}, []
    paid_at = datetime.fromtimestamp(app.clock[0], timezone.utc).isoformat()

    def transport(method, url, data, headers):
        if not url.startswith("https://gate.lava.top"):
            return original(method, url, data, headers)
        calls.append(method)
        if method == "POST":
            return {"id": "followthrough-invoice", "paymentUrl": "https://example.test/pay"}
        if state["mode"] == "pending":
            return {"id": "followthrough-invoice", "status": "pending"}
        return {
            "id": "followthrough-invoice",
            "status": "completed",
            "datetime": paid_at,
            "receipt": {
                "amount": "invalid" if state["mode"] == "invalid" else 1.23,
                "currency": "RUB",
            },
        }

    app.config["HTTP_TRANSPORT"] = transport
    event = {
        "eventType": "payment.success",
        "contractId": "followthrough-invoice",
        "amount": 1.23,
        "currency": "RUB",
        "timestamp": paid_at,
        "status": "completed",
    }
    return owner, order, state, calls, event, files


def callback(app, event):
    res = app.test_client().post(
        "/api/webhooks/lava", headers={"X-Api-Key": "fixture-webhook"}, json=event
    )
    assert res.status_code == 200, res.json


def test_invoice_matched_after_callback_resolves_only_its_reconciliation(app):
    owner, order, state, calls, event, files = lava_sale(app)
    callback(app, event)
    drain(app)
    with connect(app.config["DATABASE"]) as db:
        issue = unpack(
            db.execute(
                "SELECT * FROM objects WHERE kind='reconciliation' AND status='open'"
            ).fetchone()
        )
        assert issue["data"]["issue"] == "unmatched_lava_invoice"
    assert (
        owner.post(
            f"/api/orders/{order['id']}/checkout", json={"email": "buyer@example.test"}
        ).status_code
        == 202
    )
    state["mode"] = "completed"
    drain(app)
    app.clock[0] += 60
    drain(app)
    with connect(app.config["DATABASE"]) as db:
        resolved = unpack(db.execute("SELECT * FROM objects WHERE id=?", (issue["id"],)).fetchone())
        assert resolved["status"] == "resolved"
        assert (
            db.execute(
                "SELECT count(*) FROM audit WHERE object_id=? AND action='payment.reconciliation_resolved'",
                (issue["id"],),
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert calls.count("POST") == 1
    assert len(files) == 1


@pytest.mark.parametrize("mode", ["invalid", "pending"])
def test_payment_retries_distinguish_invalid_from_nonterminal_receipts(app, mode):
    owner, order, state, calls, event, files = lava_sale(app)
    assert (
        owner.post(
            f"/api/orders/{order['id']}/checkout", json={"email": "buyer@example.test"}
        ).status_code
        == 202
    )
    drain(app)
    state["mode"] = mode
    callback(app, event)
    for _ in range(10):
        drain(app)
        app.clock[0] += 3600
    with connect(app.config["DATABASE"]) as db:
        job = dict(db.execute("SELECT * FROM outbox WHERE event='payment.process'").fetchone())
    assert job["status"] == ("failed" if mode == "invalid" else "pending")
    assert job["attempts"] == (8 if mode == "invalid" else 10)
    if mode == "pending":
        state["mode"] = "completed"
        drain(app)
        assert len(files) == 1
    assert calls.count("POST") == 1


def test_specialist_queue_orders_all_records_before_pagination(app):
    admin = login(app)
    with transaction(app.config["DATABASE"]) as db:
        for i in range(201):
            insert(
                db, "report", {"due_at": app.clock[0] + 1000 + i}, app.clock[0] + i, status="draft"
            )
        urgent = insert(
            db, "report", {"due_at": app.clock[0] - 1}, app.clock[0] + 202, status="draft"
        )
    response = admin.get("/api/admin/queue").json
    assert response["items"][0]["id"] == urgent["id"]
    assert response["items"][0]["overdue"] is True
    assert response["total"] == 202
    second = admin.get("/api/admin/queue?offset=200").json
    assert len(second["items"]) == 2
    assert set(o["id"] for o in response["items"]).isdisjoint(o["id"] for o in second["items"])


def test_telegram_queue_exposes_next_page_without_dropping_records(app):
    admin = login(app)
    with transaction(app.config["DATABASE"]) as db:
        for i in range(35):
            insert(db, "report", {"due_at": app.clock[0] + i}, app.clock[0] + i, status="draft")
    sent, _ = capture(app)
    bot = Bot(app, {"id": 1, "first_name": "Specialist"}, 1, "queue-page")
    bot.action("queue", {})
    buttons = [b for row in sent[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert len(buttons) == 31
    next_button = next(b for b in buttons if "Далее" in b["text"])
    with connect(app.config["DATABASE"]) as db:
        action = json.loads(
            db.execute(
                "SELECT data FROM objects WHERE id=?", (next_button["callback_data"][2:],)
            ).fetchone()[0]
        )
    bot.action(action.pop("action"), action)
    buttons = [b for row in sent[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert len([b for b in buttons if b["text"].startswith("report")]) == 5
    assert any("Назад" in b["text"] for b in buttons)


def test_prior_guide_does_not_replace_red_result_diagnostic_actions(app):
    _, owner, _, _, _ = repeat_buyer(app)
    red = finish(owner, start(owner, key="paid-buyer-red"), ["c"] * 6)
    result = owner.get(f"/api/funnel-runs/{red['id']}").json
    actions = [button[1] for button in result["data"]["result_buttons"]]
    assert actions == ["funnel_consultation", "funnel_book"]
