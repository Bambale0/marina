"""Personal specialist PDFs after a completed, genuinely paid consultation."""

import io

import pytest
from reportlab.pdfgen import canvas

from marina.db import connect
from marina.operations import tick
from tests.test_consultation_commerce import app, pay_consultation, quote
from tests.test_consultation_remainder import journey_sale
from tests.test_funnel import drain
from tests.test_identity import login
from tests.test_screening import publish


CASES = [
    ("3-4", 600000, 3900, 24, 2),
    ("5-6", 600000, 3900, 24, 5),
    ("7-9", 650000, 3900, 24, 9),
    ("10-14", 700000, 4500, None, 13),
]


def completed_consultation(app, group="3-4", base=600000, promo=3900):
    admin, owner, _, booking, sent, files, _ = journey_sale(app, group, base, promo)
    q = quote(owner, booking)
    assert q.status_code == 201, q.json
    order = owner.post("/api/orders", json={"quote_id": q.json["id"], "key": "result-sale"})
    assert order.status_code == 200, order.json
    assert pay_consultation(app, owner, order.json).status_code == 200
    drain(app)
    booking = owner.get("/api/bookings/" + booking["id"]).json
    proposed = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/propose",
        json={
            "revision": booking["revision"],
            "starts": app.clock[0] + 60,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
            "reason": "Synthetic appointment for specialist result",
        },
    )
    assert proposed.status_code == 200, proposed.json
    confirmed = owner.post(
        "/api/bookings/" + booking["id"] + "/confirm",
        json={"proposal_id": proposed.json["data"]["current_proposal"]},
    )
    assert confirmed.status_code == 200, confirmed.json
    app.clock[0] += 3660
    completed = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/complete",
        json={"revision": confirmed.json["revision"], "reason": "Specialist confirms held call"},
    )
    assert completed.status_code == 200, completed.json
    result = owner.get("/api/bookings/" + booking["id"] + "/result")
    assert result.status_code == 200, result.json
    return admin, owner, completed.json, result.json, sent, files


def result_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(40, 750, "Synthetic specialist result for integration testing only")
    pdf.save()
    return stream.getvalue()


def upload_result(admin, result, raw=None):
    response = admin.post(
        "/api/admin/consultation-results/" + result["id"] + "/media",
        data={
            "reason": "Specialist uploads personal PDF",
            "file": (io.BytesIO(raw or result_pdf()), "result.pdf", "application/pdf"),
        },
    )
    assert response.status_code == 201, response.json
    return response.json


def publish_result(admin, result, media):
    response = admin.post(
        "/api/admin/consultation-results/" + result["id"] + "/publish",
        json={
            "revision": result["revision"],
            "attachment_id": media["id"],
            "reason": "Specialist approves the personal result",
        },
    )
    assert response.status_code == 200, response.json
    return response.json


@pytest.mark.parametrize("group,base,promo,hours,page", CASES)
def test_completed_call_creates_versioned_result_obligation_once(
    app, group, base, promo, hours, page
):
    admin, owner, booking, result, _, _ = completed_consultation(app, group, base, promo)
    assert booking["data"]["result_id"] == result["id"]
    assert result["status"] == "in_progress"
    assert result["data"]["booking_id"] == booking["id"]
    assert result["data"]["completed_at"] == app.clock[0]
    assert result["data"]["due_at"] == (app.clock[0] + hours * 3600 if hours else None)
    assert result["data"]["result_delivery"]["source_page"] == page
    assert (
        result["data"]["result_delivery"] == booking["data"]["journey_snapshot"]["result_delivery"]
    )
    assert result["data"]["answer_attachments"] == []
    assert owner.get("/api/consultation-results/" + result["id"] + "/pdf").status_code == 404
    assert result["id"] in [x["id"] for x in owner.get("/api/history").json["items"]]
    assert result["id"] in [x["id"] for x in admin.get("/api/admin/queue").json["items"]]
    replay = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/complete",
        json={
            "revision": booking["revision"] - 1,
            "reason": "Retry completion after response loss",
        },
    )
    assert replay.status_code == 200, replay.json
    assert replay.json["data"]["result_id"] == result["id"]
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM objects WHERE kind='consultation_result'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE event='consultation_result.created'"
            ).fetchone()[0]
            == 1
        )


def test_personal_pdf_is_private_until_specialist_publishes_and_then_delivered(app):
    admin, owner, _, result, _, files = completed_consultation(app)
    stranger = login(app, 3)
    raw = result_pdf()
    media = upload_result(admin, result, raw)
    assert owner.get("/api/media/" + media["id"]).status_code == 404
    assert stranger.get("/api/consultation-results/" + result["id"]).status_code == 404
    assert (
        owner.post(
            "/api/admin/consultation-results/" + result["id"] + "/publish",
            json={
                "revision": result["revision"],
                "attachment_id": media["id"],
                "reason": "Not specialist",
            },
        ).status_code
        == 403
    )
    published = publish_result(admin, result, media)
    assert published["status"] == "delivery_pending"
    assert published["data"]["delivered_at"] is None
    assert owner.get("/api/media/" + media["id"]).data == raw
    assert owner.get("/api/consultation-results/" + result["id"] + "/pdf").data == raw
    assert stranger.get("/api/media/" + media["id"]).status_code == 404
    assert stranger.get("/api/consultation-results/" + result["id"] + "/pdf").status_code == 404
    before = len(files)
    drain(app)
    delivered = owner.get("/api/consultation-results/" + result["id"]).json
    assert delivered["status"] == "completed"
    assert delivered["data"]["delivered_at"] == app.clock[0]
    assert len(files) == before + 1
    assert files[-1][1] == result["data"]["filename"]
    assert files[-1][2] == raw
    again = publish_result(admin, result, media)
    assert again["id"] == result["id"]
    drain(app)
    assert len(files) == before + 1
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE event='consultation_result.published'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE object_id=? AND action='consultation_result.delivered'",
                (result["id"],),
            ).fetchone()[0]
            == 1
        )


def test_failed_delivery_retries_without_losing_private_pdf_or_completing_early(app):
    admin, owner, _, result, _, files = completed_consultation(app)
    media = upload_result(admin, result)
    publish_result(admin, result, media)
    original = app.config["FILE_TRANSPORT"]
    app.config["FILE_TRANSPORT"] = lambda *args: (_ for _ in ()).throw(TimeoutError("synthetic"))
    drain(app)
    pending = owner.get("/api/consultation-results/" + result["id"]).json
    assert pending["status"] == "delivery_pending"
    assert pending["data"]["delivered_at"] is None
    assert owner.get("/api/consultation-results/" + result["id"] + "/pdf").status_code == 200
    with connect(app.config["DATABASE"]) as db:
        event = dict(
            db.execute(
                "SELECT * FROM outbox WHERE event='consultation_result.published'"
            ).fetchone()
        )
        assert event["status"] == "pending"
        assert (
            db.execute(
                "SELECT outcome FROM deliveries WHERE event_id=? ORDER BY id DESC", (event["id"],)
            ).fetchone()[0]
            == "failed"
        )
    app.config["FILE_TRANSPORT"] = original
    app.clock[0] += 100
    before = len(files)
    drain(app)
    assert owner.get("/api/consultation-results/" + result["id"]).json["status"] == "completed"
    assert len(files) == before + 1


def test_result_overdue_queue_uses_saved_deadline(app):
    admin, _, _, result, _, _ = completed_consultation(app)
    publish(
        admin,
        "policy",
        {
            "type": "operations",
            "terms": "Synthetic reminders",
            "sla_reminder_hours": [0],
            "renewal_reminder_hours": [],
        },
    )
    app.clock[0] = result["data"]["due_at"] + 1
    tick(app)
    tick(app)
    queued = next(x for x in admin.get("/api/admin/queue").json["items"] if x["id"] == result["id"])
    assert queued["overdue"] is True
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE object_id=? AND event='sla.overdue'",
                (result["id"],),
            ).fetchone()[0]
            == 1
        )


def test_result_upload_rejects_non_pdf_and_obeys_existing_privacy_size_limit(app):
    admin, _, _, result, _, _ = completed_consultation(app)
    url = "/api/admin/consultation-results/" + result["id"] + "/media"
    for raw, mime in [
        (b"ID3not-a-pdf", "audio/mpeg"),
        (b"not-a-pdf", "application/pdf"),
        (b"%PDF-" + b"x" * 1000000, "application/pdf"),
    ]:
        response = admin.post(
            url,
            data={"reason": "Reject unsafe result", "file": (io.BytesIO(raw), "result.pdf", mime)},
        )
        assert response.status_code == 422, response.json


def test_personal_result_uses_existing_retention_workflow_after_delivery(app):
    admin, _, _, result, _, _ = completed_consultation(app)
    media = upload_result(admin, result)
    publish_result(admin, result, media)
    drain(app)
    app.clock[0] = media["data"]["expires"] + 1
    tick(app)
    admin = login(app)
    owner = login(app, 2)
    with connect(app.config["DATABASE"]) as db:
        retention = db.execute(
            "SELECT * FROM objects WHERE kind='retention_task' AND json_extract(data,'$.media_id')=?",
            (media["id"],),
        ).fetchone()
        assert retention is not None
    deleted = admin.post(
        "/api/admin/media/" + media["id"] + "/delete",
        json={"revision": media["revision"], "reason": "Retention period has expired"},
    )
    assert deleted.status_code == 200, deleted.json
    assert owner.get("/api/consultation-results/" + result["id"] + "/pdf").status_code == 404
    assert owner.get("/api/media/" + media["id"]).status_code == 404
