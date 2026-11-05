"""A paid consultation creates a manual first-contact obligation in working minutes."""

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from marina.db import connect
from marina.operations import tick
from tests.test_consultation_commerce import (
    app as consultation_app,
    confirm_booking,
    pay_consultation,
    quote,
)
from tests.test_consultation_remainder import journey_sale
from tests.test_funnel import drain, finish, scenario, start

app = consultation_app
SERVICE = {
    "version": "author-remainder-20261105",
    "timezone": "Europe/Moscow",
    "start_hour": 9,
    "end_hour": 21,
    "due_minutes": 30,
    "source_page": 3,
}


def moscow(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Europe/Moscow")).timestamp())


def request_with_contact(app, service=SERVICE, admin_access=False):
    admin, owner, run, _, sent, files, journey = journey_sale(app, book=False)
    journey = copy.deepcopy(journey)
    if service is None:
        journey.pop("contact_service", None)
    else:
        journey["contact_service"] = copy.deepcopy(service)
    snapshot = admin.get("/api/admin/objects/" + run["id"]).json["object"]["data"]["snapshot"]
    scenario(
        admin,
        "3-4",
        guide_product_id=snapshot["guide_product_id"],
        consultation_product_id=snapshot["consultation_product_id"],
        consultation_journey=journey,
        contact_url=snapshot["contact_url"],
    )
    buyer = admin if admin_access else owner
    run = finish(buyer, start(buyer, key="contact-obligation"), ["b"] * 6)
    response = buyer.post(
        "/api/funnel-runs/" + run["id"] + "/book",
        json={
            "answers": {
                "profile": "Synthetic child profile",
                "problem": "custom:Synthetic question",
                "slot": "option_1",
            }
        },
    )
    assert response.status_code == 200, response.json
    return admin, buyer, response.json, journey, sent, files


def purchase(app, buyer, booking):
    calculation = quote(buyer, booking)
    assert calculation.status_code == 201, calculation.json
    response = buyer.post(
        "/api/orders", json={"quote_id": calculation.json["id"], "key": "contact-payment"}
    )
    assert response.status_code == 200, response.json
    order = response.json
    if order["status"] != "paid":
        paid = pay_consultation(app, buyer, order)
        assert paid.status_code == 200, paid.json
    return order, buyer.get("/api/bookings/" + booking["id"]).json


def contact_events(app, booking_id):
    with connect(app.config["DATABASE"]) as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM outbox WHERE object_id=? AND event='consultation.contact_due'",
                (booking_id,),
            )
        ]


@pytest.mark.parametrize(
    "paid_local,expected_local",
    [
        ("2027-01-16T09:00:00", "2027-01-16T09:30:00"),
        ("2027-01-16T08:30:00", "2027-01-16T09:30:00"),
        ("2027-01-16T20:30:00", "2027-01-16T21:00:00"),
        ("2027-01-16T20:45:00", "2027-01-17T09:15:00"),
        ("2027-01-16T21:00:00", "2027-01-17T09:30:00"),
        ("2027-01-16T23:45:00", "2027-01-17T09:30:00"),
    ],
)
def test_real_payment_boundary_creates_thirty_working_minute_due(app, paid_local, expected_local):
    paid_at = moscow(paid_local)
    app.clock[0] = paid_at - 3600
    _, buyer, booking, _, _, _ = request_with_contact(app)
    assert "contact_due_at" not in booking["data"]
    app.clock[0] = paid_at
    _, paid = purchase(app, buyer, booking)
    assert paid["data"]["paid_at"] == paid_at
    assert paid["data"].get("contact_due_at") == moscow(expected_local)
    assert paid["data"]["contact_service"] == SERVICE
    assert paid["data"].get("contacted_at") is None


def test_repeated_payment_keeps_contact_deadline_and_booking_version_snapshot(app):
    admin, buyer, booking, journey, _, _ = request_with_contact(app)
    order, paid = purchase(app, buyer, booking)
    expected_due = paid["data"].get("contact_due_at")
    assert expected_due is not None
    journey["contact_service"] = {**SERVICE, "version": "later-rules", "due_minutes": 90}
    snapshot = order["data"]["booking_snapshot"]["data"]
    run = admin.get("/api/admin/objects/" + snapshot["run_id"]).json["object"]
    scenario(
        admin,
        "3-4",
        guide_product_id=run["data"]["snapshot"]["guide_product_id"],
        consultation_product_id=run["data"]["snapshot"]["consultation_product_id"],
        consultation_journey=journey,
        contact_url=run["data"]["snapshot"]["contact_url"],
    )
    app.clock[0] += 600
    assert pay_consultation(app, buyer, order).status_code == 200
    current = buyer.get("/api/bookings/" + booking["id"]).json
    assert current["revision"] == paid["revision"]
    assert current["data"]["contact_due_at"] == expected_due
    assert current["data"]["contact_service"] == SERVICE


def test_legacy_booking_without_contact_rules_is_not_activated_by_later_publication(app):
    admin, buyer, booking, journey, _, _ = request_with_contact(app, service=None)
    journey["contact_service"] = SERVICE
    run = admin.get("/api/admin/objects/" + booking["data"]["run_id"]).json["object"]
    scenario(
        admin,
        "3-4",
        guide_product_id=run["data"]["snapshot"]["guide_product_id"],
        consultation_product_id=run["data"]["snapshot"]["consultation_product_id"],
        consultation_journey=journey,
        contact_url=run["data"]["snapshot"]["contact_url"],
    )
    _, paid = purchase(app, buyer, booking)
    assert "contact_due_at" not in paid["data"]
    app.clock[0] += 2 * 86400
    tick(app)
    assert contact_events(app, booking["id"]) == []


def test_admin_access_does_not_start_a_paid_contact_sla(app):
    _, buyer, booking, _, _, _ = request_with_contact(app, admin_access=True)
    order, paid = purchase(app, buyer, booking)
    assert order["data"]["access_source"] == "admin"
    assert "contact_due_at" not in paid["data"]
    app.clock[0] += 2 * 86400
    tick(app)
    assert contact_events(app, booking["id"]) == []
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT count(*) FROM receipts WHERE order_id=?", (order["id"],)).fetchone()[
                0
            ]
            == 0
        )


def test_manual_contact_requires_admin_reason_revision_and_does_not_send_a_message(app):
    admin, buyer, booking, _, sent, _ = request_with_contact(app)
    path = "/api/admin/bookings/" + booking["id"] + "/contacted"
    assert (
        admin.post(path, json={"revision": booking["revision"], "reason": "Unpaid"}).status_code
        == 422
    )
    _, paid = purchase(app, buyer, booking)
    drain(app)
    assert (
        buyer.post(path, json={"revision": paid["revision"], "reason": "Client"}).status_code == 403
    )
    assert (
        admin.post(
            path, json={"revision": booking["revision"], "reason": "Old version"}
        ).status_code
        == 409
    )
    assert admin.post(path, json={"revision": paid["revision"]}).status_code == 422
    messages_before = len(sent)
    response = admin.post(
        path, json={"revision": paid["revision"], "reason": "Spoke personally in Telegram"}
    )
    assert response.status_code == 200, response.json
    contacted = response.json
    assert contacted["data"]["contacted_at"] == app.clock[0]
    assert contacted["data"]["contacted_by"] == admin.get("/api/me").json["account"]["id"]
    assert len(sent) == messages_before
    repeat = admin.post(
        path, json={"revision": contacted["revision"], "reason": "Same contact acknowledgement"}
    )
    assert repeat.status_code == 200, repeat.json
    assert repeat.json["revision"] == contacted["revision"]
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM audit WHERE action='booking.contacted' AND object_id=?",
                (booking["id"],),
            ).fetchone()[0]
            == 1
        )
    app.clock[0] = contacted["data"]["contact_due_at"] + 1
    tick(app)
    assert contact_events(app, booking["id"]) == []


def test_contact_queue_includes_confirmed_booking_until_manually_marked(app):
    admin, buyer, booking, _, _, _ = request_with_contact(app)
    _, paid = purchase(app, buyer, booking)
    assert paid["data"].get("contact_due_at") is not None
    confirmed = confirm_booking(app, admin, buyer, paid)
    app.clock[0] = confirmed["data"]["contact_due_at"] + 1
    queue = admin.get("/api/admin/queue").json["items"]
    item = next(row for row in queue if row["id"] == booking["id"])
    assert item["due_at"] == confirmed["data"]["contact_due_at"] and item["overdue"] is True
    response = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/contacted",
        json={"revision": confirmed["revision"], "reason": "Personally contacted parent"},
    )
    assert response.status_code == 200, response.json
    assert booking["id"] not in {row["id"] for row in admin.get("/api/admin/queue").json["items"]}


@pytest.mark.parametrize("close_action", ["contacted", "cancel"])
def test_queued_contact_reminder_is_suppressed_after_contact_or_cancellation(app, close_action):
    admin, buyer, booking, _, sent, _ = request_with_contact(app)
    _, paid = purchase(app, buyer, booking)
    assert paid["data"].get("contact_due_at") is not None
    drain(app)
    app.clock[0] = paid["data"]["contact_due_at"]
    tick(app)
    tick(app)
    assert len(contact_events(app, booking["id"])) == 1
    response = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/" + close_action,
        json={"revision": paid["revision"], "reason": "Synthetic resolution before delivery"},
    )
    assert response.status_code == 200, response.json
    before = len(sent)
    drain(app)
    assert not any("Личный контакт после оплаты" in row.get("text", "") for _, row in sent[before:])
    tick(app)
    assert len(contact_events(app, booking["id"])) == 1
    if close_action == "cancel":
        assert (
            admin.post(
                "/api/admin/bookings/" + booking["id"] + "/contacted",
                json={"revision": response.json["revision"], "reason": "Cancelled booking"},
            ).status_code
            == 409
        )


def test_due_contact_reminder_is_sent_to_specialist_once_not_to_client(app):
    admin, buyer, booking, _, sent, _ = request_with_contact(app)
    _, paid = purchase(app, buyer, booking)
    assert paid["data"].get("contact_due_at") is not None
    drain(app)
    app.clock[0] = paid["data"]["contact_due_at"] - 1
    tick(app)
    assert contact_events(app, booking["id"]) == []
    app.clock[0] += 1
    tick(app)
    tick(app)
    drain(app)
    messages = [row for _, row in sent if "Личный контакт после оплаты" in row.get("text", "")]
    assert len(messages) == 1 and messages[0]["chat_id"] == 1
    assert contact_events(app, booking["id"])[0]["status"] == "sent"
    assert buyer.get("/api/bookings/" + booking["id"]).json["data"].get("contacted_at") is None
