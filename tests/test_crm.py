import io
import subprocess

import pytest

from marina.operations import work_once
from tests.test_identity import app, login
from tests.test_screening import publish, subject


def setup_review(app):
    admin, client = login(app), login(app, 2)
    app.config["ENFORCE_CONSENT"] = True
    policy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Fixture consent",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    client.post("/api/consents", json={"policy_id": policy["id"], "accepted": True})
    child = subject(client, "child")
    res = client.post(
        "/api/video-reviews",
        json={
            "subject_id": child["id"],
            "concern": {
                "text": "Fixture problem",
                "selected_activity": {"id": "1", "title": "Fixture task"},
            },
            "key": "review",
        },
    )
    assert res.status_code == 201, res.json
    return admin, client, res.json


def test_video_review_crm_lifecycle_and_isolation(app, tmp_path):
    admin, client, lead = setup_review(app)
    assert login(app, 3).get("/api/video-reviews/" + lead["id"]).status_code == 404
    assert client.get("/api/admin/crm").status_code == 403


def make_video(tmp_path, codec="libx264"):
    path = tmp_path / "review.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=32x32:d=0.2",
            "-an",
            "-c:v",
            codec,
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        timeout=15,
    )
    return path.read_bytes()


@pytest.mark.parametrize("assigned", [False, True])
def test_specialist_video_retry_and_recipient_access(app, tmp_path, assigned):
    app.config["ADMIN_TELEGRAM_IDS"] = {1, 4, 5}
    admin, client, lead = setup_review(app)
    second = login(app, 4).get("/api/me").json["account"]["id"]
    blocked = login(app, 5).get("/api/me").json["account"]["id"]
    admin.post("/api/admin/accounts/" + blocked + "/block", json={"reason": "Access revoked"})
    if assigned:
        assert (
            admin.post(
                "/api/admin/crm/" + lead["id"] + "/assign",
                json={
                    "revision": lead["revision"],
                    "assigned_to": second,
                    "reason": "Fixture assignment",
                },
            ).status_code
            == 200
        )
    raw = make_video(tmp_path)
    assert (
        client.post(
            "/api/video-reviews/" + lead["id"] + "/video",
            data={"file": (io.BytesIO(raw), "review.mp4", "video/mp4")},
        ).status_code
        == 201
    )
    files, notices = [], []
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)

    def deliver(method, url, data, headers):
        if data["chat_id"] == 4:
            raise RuntimeError("Temporary specialist notice failure")
        notices.append(data)
        return {"ok": True, "result": {}}

    app.config["HTTP_TRANSPORT"] = deliver
    work_once(app)
    expected = [4] if assigned else [1, 4]
    assert [f[0] for f in files] == expected
    assert all(f[2] == raw for f in files)
    app.clock[0] += 30
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: notices.append(data) or {
        "ok": True,
        "result": {},
    }
    work_once(app)
    assert [f[0] for f in files] == expected  # Confirmed uploads are not repeated on notice retry.
    assert [n["chat_id"] for n in notices] == expected
    button = notices[-1]["reply_markup"]["inline_keyboard"][0][0]
    from marina.bot import handle_update

    def click(uid):
        handle_update(
            app,
            {
                "update_id": 4000 + uid,
                "callback_query": {
                    "id": "staff-card",
                    "from": {"id": uid},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": uid, "type": "private"}},
                },
            },
        )

    click(2)
    assert [f[0] for f in files] == expected
    click(4)
    assert files[-1][0] == 4 and lead["id"] in notices[-1]["text"]
    assert any(
        b["text"] == "Написать мини-заключение"
        for row in notices[-1]["reply_markup"]["inline_keyboard"]
        for b in row
    )


def test_real_video_to_specialist_and_diagnostic_booking(app, tmp_path):
    admin, client, public = setup_review(app)
    review_id = public["id"]
    raw = make_video(tmp_path)
    uploaded = client.post(
        "/api/video-reviews/" + review_id + "/video",
        data={"file": (io.BytesIO(raw), "review.mp4", "video/mp4"), "note": "Parent caption"},
    )
    assert uploaded.status_code == 201, uploaded.json
    assert uploaded.json["status"] == "video_received"
    video_id = uploaded.json["data"]["video_id"]
    assert client.get("/api/media/" + video_id).data == raw
    assert login(app, 3).get("/api/media/" + video_id).status_code == 404
    assert admin.get("/api/media/" + video_id).data == raw
    assert (
        client.post(
            "/api/video-reviews/" + review_id + "/video",
            data={"file": (io.BytesIO(raw), "same.mp4", "video/mp4")},
        ).status_code
        == 200
    )
    assert len(admin.get("/api/admin/crm").json["items"]) == 1
    assert any(o["id"] == review_id for o in admin.get("/api/admin/queue").json["items"])
    sent = []
    files = []
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    work_once(app)  # Staff notification precedes the independently tested client send.
    assert files == [(1, "client-video.mp4", raw, "video/mp4", "sendVideo")]
    notice = sent[-1]
    assert notice["chat_id"] == 1 and "Fixture task" in notice["text"]
    review_button = notice["reply_markup"]["inline_keyboard"][0][0]
    assert review_button["text"] == "Открыть обращение"

    def card():
        return admin.get("/api/admin/crm/" + review_id).json["object"]

    def act(action, **data):
        obj = card()
        response = admin.post(
            "/api/admin/crm/" + review_id + "/" + action,
            json={"revision": obj["revision"], "reason": "Specialist review fixture", **data},
        )
        assert response.status_code == 200, response.json
        return response.json

    act("assign", assigned_to=admin.get("/api/me").json["account"]["id"])
    act("comment", text="Internal specialist note")
    act("next-contact", next_contact=app.clock[0] + 3600)
    act("take")
    act("conclusion", text="Mini-conclusion written by specialist.")
    forbidden = admin.post(
        "/api/admin/crm/" + review_id + "/send-conclusion",
        json={"revision": card()["revision"], "reason": "No confirmed viewing"},
    )
    assert forbidden.status_code == 422
    act("viewed")
    stale = card()["revision"]
    act("conclusion", text="Approved exact mini-conclusion.")
    assert (
        admin.post(
            "/api/admin/crm/" + review_id + "/comment",
            json={"revision": stale, "text": "stale edit", "reason": "Conflict fixture"},
        ).status_code
        == 409
    )
    act("send-conclusion")
    assert card()["status"] == "in_review"
    assert client.get("/api/video-reviews/" + review_id).json["data"]["conclusion"] is None
    app.config["HTTP_TRANSPORT"] = lambda *args: (_ for _ in ()).throw(RuntimeError("network down"))
    work_once(app)
    assert card()["status"] == "in_review"
    assert card()["data"]["pending_message_id"]
    app.clock[0] += 30
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    work_once(app)
    assert card()["status"] == "conclusion_sent"
    assert sent[-1]["text"] == "Approved exact mini-conclusion."
    published = client.get("/api/video-reviews/" + review_id).json
    assert "comments" not in published["data"] and "conclusion_draft" not in published["data"]
    assert "Internal specialist note" not in str(client.get("/api/history").json)
    act("invite", text="Invite to full diagnostic assessment.")
    assert card()["status"] == "conclusion_sent"
    work_once(app)
    assert card()["status"] == "invited"
    button = sent[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Записаться на диагностику" and button["style"] == "success"
    from marina.bot import handle_update

    handle_update(
        app,
        {
            "update_id": 1001,
            "callback_query": {
                "id": "invite",
                "from": {"id": 2},
                "data": button["callback_data"],
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    assert "Какие дни и время" in sent[-1]["text"]
    handle_update(
        app,
        {
            "update_id": 1002,
            "message": {
                "from": {"id": 2},
                "chat": {"id": 2, "type": "private"},
                "text": "Evening, Moscow",
            },
        },
    )
    booking_id = client.get("/api/video-reviews/" + review_id).json["data"]["booking_id"]
    booking = client.get("/api/bookings/" + booking_id).json
    assert "id" in booking, booking
    assert (
        client.post(
            "/api/video-reviews/" + review_id + "/book", json={"preferences": "duplicate"}
        ).json["id"]
        == booking["id"]
    )
    assert card()["status"] == "invited"
    proposal = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/propose",
        json={
            "revision": booking["revision"],
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
            "reason": "Agree full diagnostic time",
        },
    ).json
    assert proposal["status"] == "proposed", proposal
    while work_once(app):
        pass
    proposal_message = next(m for m in sent if m.get("text", "").startswith("Полная диагностика:"))
    confirm_button = proposal_message["reply_markup"]["inline_keyboard"][0][0]
    handle_update(
        app,
        {
            "update_id": 1003,
            "callback_query": {
                "id": "confirm",
                "from": {"id": 2},
                "data": confirm_button["callback_data"],
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    confirmed = client.post(
        "/api/bookings/" + booking["id"] + "/confirm",
        json={"proposal_id": proposal["data"]["current_proposal"]},
    ).json
    assert confirmed["status"] == "confirmed"
    assert card()["status"] == "booked"
    complete = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/complete",
        json={"revision": confirmed["revision"], "reason": "Full diagnostics conducted"},
    ).json
    assert complete["status"] == "completed"
    assert card()["status"] == "completed"
    assert client.post("/api/bookings/" + booking["id"] + "/reschedule", json={}).status_code == 409
    assert card()["status"] == "completed"
    assert admin.get("/api/admin/crm/" + review_id).json["audit"]
    from marina.operations import tick

    app.clock[0] += 31 * 86400
    tick(app)
    admin = login(app)
    media = admin.get("/api/admin/objects/" + video_id).json["object"]
    assert any(o["kind"] == "retention_task" for o in admin.get("/api/admin/queue").json["items"])
    deleted = admin.post(
        "/api/admin/media/" + video_id + "/delete",
        json={
            "revision": media["revision"],
            "reason": "Retention elapsed after completed diagnostics",
        },
    )
    assert deleted.status_code == 200, deleted.json
    assert admin.get("/api/media/" + video_id).status_code == 404


def test_video_requires_consent_valid_mp4_and_keeps_private_data(app, tmp_path):
    admin, client, lead = setup_review(app)
    url = "/api/video-reviews/" + lead["id"] + "/video"
    assert (
        client.post(
            url, data={"file": (io.BytesIO(b"notvideo"), "video.mp4", "video/mp4")}
        ).status_code
        == 422
    )
    raw = make_video(tmp_path)
    assert (
        client.post(
            url, data={"file": (io.BytesIO(raw[:8] + b"broken"), "video.mp4", "video/mp4")}
        ).status_code
        == 422
    )
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "New consent revision",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    assert (
        client.post(url, data={"file": (io.BytesIO(raw), "video.mp4", "video/mp4")}).status_code
        == 422
    )
    assert admin.get("/api/admin/crm/" + lead["id"]).json["object"]["status"] == "awaiting_video"


def test_crm_followup_reminder_once_and_staff_assignment(app):
    from marina.db import connect
    from marina.operations import tick

    admin, client, lead = setup_review(app)
    customer_id = client.get("/api/me").json["account"]["id"]
    route = "/api/admin/crm/" + lead["id"]
    assert (
        admin.post(
            route + "/assign",
            json={
                "revision": lead["revision"],
                "assigned_to": customer_id,
                "reason": "Reject non-specialist",
            },
        ).status_code
        == 422
    )
    changed = admin.post(
        route + "/next-contact",
        json={
            "revision": lead["revision"],
            "next_contact": app.clock[0] + 60,
            "reason": "Follow up on pending video",
        },
    ).json
    assert changed["data"]["next_contact"] == app.clock[0] + 60
    app.clock[0] += 61
    tick(app)
    tick(app)
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE object_id=? AND event='crm.contact_due'",
                (lead["id"],),
            ).fetchone()[0]
            == 1
        )
