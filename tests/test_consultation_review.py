"""Regression tests for consultation/video-task release review boundaries."""

import copy
import io
import pytest
from marina import Problem
from marina.bot import Bot
from marina.db import connect, transaction
from marina.consultation_delivery import notify_specialist_event
from marina.operations import tick
from tests.test_consultation_remainder import app, task_setup, route_setup, journey_sale, SOURCE
from tests.test_consultation_commerce import quote, pay_consultation
from tests.test_crm import make_video
from tests.test_screening import publish
from tests.test_identity import login


def assignment(admin, task):
    r = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Авторское задание",
            "reason": "Назначение специалиста",
        },
    )
    assert r.status_code == 200, r.json
    return r.json


def test_abandoned_task_closes_with_reason_revision_and_retention(app, tmp_path):
    admin, owner, task = task_setup(app)
    task = assignment(admin, task)
    raw = make_video(tmp_path)
    uploaded = owner.post(
        "/api/video-tasks/" + task["id"] + "/video",
        data={"slot": "1", "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
    ).json
    path = "/api/admin/video-tasks/" + task["id"] + "/close"
    assert (
        owner.post(path, json={"revision": uploaded["revision"], "reason": "Клиент"}).status_code
        == 403
    )
    assert (
        admin.post(
            path, json={"revision": task["revision"], "reason": "Старая редакция"}
        ).status_code
        == 409
    )
    assert admin.post(path, json={"revision": uploaded["revision"]}).status_code == 422
    closed = admin.post(
        path, json={"revision": uploaded["revision"], "reason": "Родитель отказался продолжать"}
    )
    assert closed.status_code == 200, closed.json
    assert closed.json["status"] == "closed" and closed.json["data"]["due_at"] is None
    assert owner.post("/api/video-tasks/" + task["id"] + "/submit", json={}).status_code == 422
    assert task["id"] not in {o["id"] for o in admin.get("/api/admin/queue").json["items"]}
    app.clock[0] += 31 * 86400
    tick(app)
    with connect(app.config["DATABASE"]) as db:
        assert db.execute(
            "SELECT 1 FROM objects WHERE kind='retention_task' AND json_extract(data,'$.case_id')=?",
            (task["id"],),
        ).fetchone()


def test_reopened_intake_does_not_prompt_or_claim_changes(app):
    _, owner, run, booking, sent, _, _ = journey_sale(app, prepay=False)
    before = len(sent)
    bot = Bot(app, {"id": 2, "first_name": "Мама"}, 2, "repeat-intake")
    bot.action("intake_start", {"id": run["id"]})
    assert not bot.state["data"].get("consultation_intake")
    messages = [d.get("text", "") for _, d in sent[before:]]
    assert any("уже" in text.lower() for text in messages), messages
    assert (
        owner.get("/api/bookings/" + booking["id"]).json["data"]["intake_answers"]["profile"]
        == "Имя ребёнка, возраст и класс"
    )


def test_response_attachment_audit_kind(app, tmp_path):
    admin, owner, task = task_setup(app)
    assignment(admin, task)
    raw = make_video(tmp_path)
    for slot in range(1, 4):
        assert (
            owner.post(
                "/api/video-tasks/" + task["id"] + "/video",
                data={"slot": str(slot), "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
            ).status_code
            == 200
        )
    owner.post("/api/video-tasks/" + task["id"] + "/submit", json={})
    r = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/media",
        data={
            "reason": "Материал специалиста",
            "file": (io.BytesIO(b"%PDF-1.4 synthetic"), "response.pdf", "application/pdf"),
        },
    )
    assert r.status_code == 201, r.json
    with connect(app.config["DATABASE"]) as db:
        assert db.execute(
            "SELECT 1 FROM audit WHERE action='video_task.response-upload'"
        ).fetchone()


def test_paid_preparation_has_private_web_fallback_when_telegram_fails(app):
    _, owner, _, booking, _, _, _ = journey_sale(app)
    path = "/api/bookings/" + booking["id"] + "/preparation"
    assert owner.get(path).status_code == 404
    q = quote(owner, booking).json
    order = owner.post("/api/orders", json={"quote_id": q["id"], "key": "fallback"}).json
    assert pay_consultation(app, owner, order).status_code == 200

    def unavailable(*args):
        raise RuntimeError("Synthetic Telegram outage")

    app.config["FILE_TRANSPORT"] = unavailable
    result = owner.get(path)
    assert result.status_code == 200, result.json
    assert result.data.startswith(b"%PDF-")
    assert login(app, 3).get(path).status_code == 404
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT 1 FROM audit WHERE action='booking.preparation-view'").fetchone()


@pytest.mark.parametrize("field", ["gift_text", "material_filename"])
def test_downloadable_route_requires_delivery_texts(app, field):
    from marina.consultation_journey import validate_routes

    admin, _, _, _, _, _, j = journey_sale(app)
    config = copy.deepcopy(__import__("json").loads(SOURCE.read_text())["outside"])
    config["routes"]["baby_0_2"]["material_id"] = j["preparation_material_id"]
    config["routes"]["baby_0_2"].pop(field, None)
    with connect(app.config["DATABASE"]) as db:
        with pytest.raises(Problem):
            validate_routes(db, config)


@pytest.mark.parametrize("custom,other", [(False, True), (True, False)])
def test_custom_answer_flag_and_option_must_agree(app, custom, other):
    from marina.consultation_journey import validate_journey

    j = copy.deepcopy(__import__("json").loads(SOURCE.read_text())["ages"]["3-4"])
    q = j["questions"][1]
    q["allow_custom"] = custom
    if not other:
        q["options"] = [o for o in q["options"] if o["id"] != "other"]
    with connect(app.config["DATABASE"]) as db:
        with pytest.raises(Problem):
            validate_journey(db, j)


def test_disabled_video_task_does_not_create_or_advertise_booking(app):
    admin, owner, _ = route_setup(app)
    publish(
        admin,
        "policy",
        {
            "type": "operations",
            "terms": "Synthetic scope policy",
            "pause_sales": False,
            "max_open_orders": 0,
            "enabled_services": ["booking", "guide"],
            "sla_reminder_hours": [],
            "renewal_reminder_hours": [],
        },
    )
    r = owner.post(
        "/api/consultation-leads",
        json={"route": "baby_0_2", "action": "booking", "key": "disabled"},
    )
    assert r.status_code == 403, r.json
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": 1},
    }
    bot = Bot(app, {"id": 2, "first_name": "Мама"}, 2, "disabled-booking")
    bot.action("outside_route", {"route": "baby_0_2"})
    with connect(app.config["DATABASE"]) as db:
        assert not db.execute("SELECT 1 FROM objects WHERE kind='consultation_lead'").fetchone()
    assert not any("outside_interest" in str(d) and "booking" in str(d) for d in calls)
    assert not any(
        "1 900" in b.get("text", "") or "1900" in b.get("text", "")
        for d in calls
        for row in d.get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    )
    with pytest.raises(Problem):
        bot.action("outside_interest", {"route": "baby_0_2", "action": "booking"})


def test_specialist_alert_survives_blocked_parent(app):
    _, _, task = task_setup(app)
    with transaction(app.config["DATABASE"]) as db:
        db.execute("UPDATE accounts SET blocked=1 WHERE id=?", (task["owner"],))
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": 1},
    }
    notify_specialist_event(app, task["owner"], task["id"], "video_task.requested")
    assert any(d.get("chat_id") == 1 and "родитель ждёт" in d.get("text", "") for d in calls)


def test_delayed_assignment_is_discarded_after_submission(app, tmp_path):
    admin, owner, task = task_setup(app)
    assignment(admin, task)
    raw = make_video(tmp_path)
    for slot in range(1, 4):
        owner.post(
            "/api/video-tasks/" + task["id"] + "/video",
            data={"slot": str(slot), "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
        )
    assert owner.post("/api/video-tasks/" + task["id"] + "/submit", json={}).status_code == 200
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": 1},
    }
    notify_specialist_event(app, task["owner"], task["id"], "video_task.assignment")
    assert not calls


def test_current_assignment_is_sent_once_and_closed_task_gets_no_stale_assignment(app):
    admin, _, task = task_setup(app)
    task = assignment(admin, task)
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": 1},
    }
    notify_specialist_event(app, task["owner"], task["id"], "video_task.assignment")
    notify_specialist_event(app, task["owner"], task["id"], "video_task.assignment")
    assert (
        len(
            [d for d in calls if d.get("chat_id") == 2 and "Авторское задание" in d.get("text", "")]
        )
        == 1
    )
    closed = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/close",
        json={"revision": task["revision"], "reason": "По просьбе родителя"},
    )
    assert closed.status_code == 200
    before = len(calls)
    notify_specialist_event(app, task["owner"], task["id"], "video_task.assignment")
    assert len(calls) == before
