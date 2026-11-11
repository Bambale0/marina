"""Video work starts only after a dynamically priced Lava payment is confirmed."""

from marina import object_get
from marina.commerce import confirm
from marina.db import connect, transaction
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
    offer = publish(
        admin, "template", {"type": "offer", "text": "Synthetic video task offer"}
    )
    publish(
        admin,
        "policy",
        {**commerce["data"], "scope": "consultation", "offer_template_id": offer["id"]},
    )
    app.config["LAVA_VIDEO_TASK_OFFER_ID"] = "video-task-dynamic-offer"
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
    assert current["data"]["paid_at"] == app.clock[0]
    assert current["data"]["order_id"] == order["id"]
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
    app.config["LAVA_VIDEO_TASK_OFFER_ID"] = ""
    response = owner.post("/api/video-tasks/" + task["id"] + "/quote", json={})
    assert response.status_code == 503, response.json
    assert "не настроена" in response.json["error"]

