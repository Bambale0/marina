"""Video work starts only after a dynamically priced Lava payment is confirmed."""

from marina import object_get
from marina.commerce import confirm
from marina.db import connect, transaction
from marina.consultation_delivery import notify_specialist_event
from tests.test_commerce import pay
from tests.test_consultation_commerce import app as consultation_app
from tests.test_consultation_remainder import task_setup
from tests.test_commerce import policy
from tests.test_screening import publish

app = consultation_app


def test_unpaid_video_task_cannot_be_assigned_or_uploaded(app):
    admin, owner, task = task_setup(app, paid=False)
    assert task["status"] == "awaiting_payment"
    denied = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={"revision": task["revision"], "text": "Задание", "reason": "Проверка оплаты"},
    )
    assert denied.status_code == 409, denied.json
    with connect(app.config["DATABASE"]) as db:
        assert not db.execute(
            "SELECT 1 FROM outbox WHERE object_id=? AND event IN "
            "('consultation.lead','video_task.requested','video_task.paid')",
            (task["id"],),
        ).fetchone()


def test_dynamic_quote_uses_task_price_and_payment_unlocks_specialist(app):
    admin, owner, task = task_setup(app, paid=False)
    commerce = policy(admin)
    offer = publish(admin, "template", {"type": "offer", "text": "Synthetic video task offer"})
    publish(
        admin,
        "policy",
        {**commerce["data"], "scope": "consultation", "offer_template_id": offer["id"]},
    )
    app.config.update(
        VIDEO_TASK_LAVA_ENABLED=True, LAVA_VIDEO_TASK_OFFER_ID="video-task-dynamic-offer"
    )
    quoted = owner.post("/api/video-tasks/" + task["id"] + "/quote", json={})
    assert quoted.status_code == 201, quoted.json
    quote = quoted.json
    assert quote["data"]["total"] == task["data"]["amount_rub"] * 100
    assert quote["data"]["product_snapshot"]["lava_offer_id"] == "video-task-dynamic-offer"
    assert quote["data"]["product_snapshot"]["service"] == "video_task"
    order_response = owner.post(
        "/api/orders", json={"quote_id": quote["id"], "key": "video-task-payment"}
    )
    assert order_response.status_code == 200, order_response.json
    order = order_response.json
    with transaction(app.config["DATABASE"]) as db:
        paid = confirm(
            db,
            object_get(db, order["id"], "order", task["owner"]),
            "lava",
            "lava-video-task-payment",
            quote["data"]["total"],
            "RUB",
            app.clock[0],
            app.clock[0],
        )
        assert paid["status"] == "paid"
    current = owner.get("/api/video-tasks/" + task["id"]).json
    assert current["status"] == "requested"
    assert current["data"]["payment_status"] == "confirmed"
    private = admin.get("/api/admin/objects/" + task["id"]).json["object"]
    assert private["data"]["paid_at"] == app.clock[0]
    assert private["data"]["order_id"] == order["id"]
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": current["revision"],
            "text": "Три игровых движения",
            "reason": "Платёж подтверждён",
        },
    )
    assert assigned.status_code == 200, assigned.json
    assert assigned.json["status"] == "awaiting_videos"
    with connect(app.config["DATABASE"]) as db:
        assert db.execute(
            "SELECT 1 FROM outbox WHERE object_id=? AND event='video_task.paid'",
            (task["id"],),
        ).fetchone()


def test_video_quote_requires_dedicated_lava_offer(app):
    _, owner, task = task_setup(app, paid=False)
    app.config.update(VIDEO_TASK_LAVA_ENABLED=True, LAVA_VIDEO_TASK_OFFER_ID="")
    response = owner.post("/api/video-tasks/" + task["id"] + "/quote", json={})
    assert response.status_code == 503, response.json
    assert "не настроена" in response.json["error"]


def video_order(app):
    admin, owner, task = task_setup(app, paid=False)
    commerce = policy(admin)
    offer = publish(admin, "template", {"type": "offer", "text": "Synthetic video checkout terms"})
    publish(
        admin,
        "policy",
        {**commerce["data"], "scope": "consultation", "offer_template_id": offer["id"]},
    )
    app.config.update(
        VIDEO_TASK_LAVA_ENABLED=True, LAVA_VIDEO_TASK_OFFER_ID="fixture-video-only-offer"
    )
    result = owner.post("/api/video-tasks/" + task["id"] + "/quote", json={})
    assert result.status_code == 201, result.json
    q = result.json
    o = owner.post("/api/orders", json={"quote_id": q["id"], "key": "fixture-video"})
    assert o.status_code == 200, o.json
    return admin, owner, task, o.json


def test_lava_webhook_confirms_once_and_parent_and_admin_are_notified_once(app):
    admin, owner, task, order = video_order(app)
    app.config["LAVA_WEBHOOK_SECRET"] = "fixture-webhook-key"
    unverified = app.test_client().post(
        "/api/webhooks/lava", json={"eventType": "payment.success", "contractId": "untrusted"}
    )
    assert unverified.status_code == 401
    assert owner.get("/api/video-tasks/" + task["id"]).json["status"] == "awaiting_payment"
    first = pay(app, order)
    assert first.status_code == 200, first.json
    second = pay(app, order)
    assert second.status_code == 200, second.json
    saved = owner.get("/api/video-tasks/" + task["id"]).json
    assert saved["status"] == "requested"
    assert saved["data"]["payment_status"] == "confirmed"
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts WHERE provider='lava'").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE object_id=? AND event='video_task.paid'",
                (task["id"],),
            ).fetchone()[0]
            == 1
        )
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: (
        sent.append((url, data)) or {"ok": True, "result": {"message_id": len(sent)}}
    )
    notify_specialist_event(app, task["owner"], task["id"], "video_task.paid")
    notify_specialist_event(app, task["owner"], task["id"], "video_task.paid")
    parent = [
        d
        for _, d in sent
        if d.get("chat_id") == 2 and "Оплата видеоразбора подтверждена" in d.get("text", "")
    ]
    assert len(parent) == 1, sent
    assert any("оплата подтверждена" in d.get("text", "") for _, d in sent if d.get("chat_id") != 2)


def test_provider_payment_after_manual_confirmation_is_reconciled_not_duplicated(app):
    admin, owner, task, order = video_order(app)
    manual = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/confirm-payment",
        json={
            "revision": task["revision"],
            "amount_rub": task["data"]["amount_rub"],
            "reference": "fixture-bank-transfer",
            "reason": "Fixture manual confirmation",
        },
    )
    assert manual.status_code == 200, manual.json
    with transaction(app.config["DATABASE"]) as db:
        result = confirm(
            db,
            object_get(db, order["id"], "order", task["owner"]),
            "lava",
            "second-real-charge",
            order["data"]["total"],
            "RUB",
            app.clock[0],
            app.clock[0],
        )
    assert result["status"] == "paid_review"
    with connect(app.config["DATABASE"]) as db:
        receipt = db.execute(
            "SELECT status FROM receipts WHERE charge=?", ("second-real-charge",)
        ).fetchone()
        assert receipt["status"] == "duplicate_payment"
        assert (
            db.execute("SELECT COUNT(*) FROM objects WHERE kind='reconciliation'").fetchone()[0]
            == 1
        )
    assert owner.get("/api/video-tasks/" + task["id"]).json["status"] == "requested"


def test_provider_payment_for_closed_task_is_recorded_without_unblocking(app):
    admin, owner, task, order = video_order(app)
    closed = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/close",
        json={"revision": task["revision"], "reason": "Fixture client cancellation"},
    )
    assert closed.status_code == 200, closed.json
    with transaction(app.config["DATABASE"]) as db:
        result = confirm(
            db,
            object_get(db, order["id"], "order", task["owner"]),
            "lava",
            "closed-task-charge",
            order["data"]["total"],
            "RUB",
            app.clock[0],
            app.clock[0],
        )
    assert result["status"] == "paid_review"
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT status FROM receipts WHERE charge=?", ("closed-task-charge",)
            ).fetchone()[0]
            == "video_task_not_payable"
        )
    assert owner.get("/api/video-tasks/" + task["id"]).json["status"] == "closed"


def test_video_quote_rejects_disabled_commercial_gate_even_with_offer(app):
    _, owner, task = task_setup(app, paid=False)
    app.config.update(
        VIDEO_TASK_LAVA_ENABLED=False, LAVA_VIDEO_TASK_OFFER_ID="valid-but-disabled-offer"
    )
    response = owner.post("/api/video-tasks/" + task["id"] + "/quote", json={})
    assert response.status_code == 503, response.json
