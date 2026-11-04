"""Booking money is retained independently of appointment eligibility."""

import json
from datetime import datetime, timezone

import pytest

from marina.db import connect
from marina.operations import work_once
from tests.test_commerce import pay, policy, prepare_lava_payment, product
from tests.test_identity import app, login
from tests.test_screening import subject


@pytest.fixture
def confirmed_booking(app):
    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        LAVA_API_KEY="fixture-key",
        LAVA_WEBHOOK_SECRET="fixture-webhook",
        PUBLIC_URL="https://fixture.test",
    )
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(
        admin,
        "booking",
        currency="RUB",
        amount=390000,
        web_sales_approved=True,
        lava_offer_id="consultation-fixture",
    )
    person = subject(owner, "child")
    response = owner.post(
        "/api/bookings",
        json={
            "subject_id": person["id"],
            "topic": "Synthetic consultation",
            "preferences": "Fixture",
            "key": "booking",
        },
    )
    assert response.status_code == 200, response.json
    booking = response.json
    proposed = admin.post(
        "/api/admin/bookings/" + booking["id"] + "/propose",
        json={
            "revision": booking["revision"],
            "starts": app.clock[0] + 3600,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
            "reason": "Synthetic proposal",
        },
    )
    assert proposed.status_code == 200, proposed.json
    response = owner.post(
        "/api/bookings/" + booking["id"] + "/confirm",
        json={"proposal_id": proposed.json["data"]["current_proposal"]},
    )
    assert response.status_code == 200, response.json
    return admin, owner, response.json, tariff, person


def booking_quote(owner, booking, tariff, person):
    return owner.post(
        "/api/quotes",
        json={
            "product_id": tariff["id"],
            "subject_ids": [person["id"]],
            "booking_id": booking["id"],
            "surface": "telegram",
        },
    )


def booking_order(owner, booking, tariff, person, key="booking-payment"):
    quote = booking_quote(owner, booking, tariff, person)
    assert quote.status_code == 201, quote.json
    response = owner.post("/api/orders", json={"quote_id": quote.json["id"], "key": key})
    assert response.status_code == 200, response.json
    return response.json


def financial_state(app):
    with connect(app.config["DATABASE"]) as db:
        return {
            "receipts": [dict(row) for row in db.execute("SELECT * FROM receipts")],
            "reconciliations": [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM objects WHERE kind='reconciliation'")
            ],
            "fulfillments": db.execute("SELECT count(*) FROM fulfillments").fetchone()[0],
        }


def test_cancelled_booking_cannot_open_checkout(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    order = booking_order(owner, booking, tariff, person)
    assert owner.post("/api/bookings/" + booking["id"] + "/cancel", json={}).status_code == 200
    response = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert response.status_code == 422, response.json


def test_cancelled_booking_cancels_queued_invoice_before_provider_call(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    order = booking_order(owner, booking, tariff, person)
    attempt = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert attempt.status_code == 202, attempt.json
    assert owner.post("/api/bookings/" + booking["id"] + "/cancel", json={}).status_code == 200
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail("Cancelled invoice reached provider")
    for _ in range(10):
        with connect(app.config["DATABASE"]) as db:
            status = db.execute(
                "SELECT status FROM objects WHERE id=?", (attempt.json["id"],)
            ).fetchone()[0]
        if status != "queued":
            break
        assert work_once(app, send=lambda *args: None) == 1
    assert status == "cancelled"
    assert financial_state(app)["reconciliations"] == []


def test_payment_after_cancellation_is_recorded_for_review_without_fulfilling(
    app, confirmed_booking
):
    _, owner, booking, tariff, person = confirmed_booking
    order = booking_order(owner, booking, tariff, person)
    prepare_lava_payment(app, order)
    assert owner.post("/api/bookings/" + booking["id"] + "/cancel", json={}).status_code == 200
    assert pay(app, order).status_code == 200
    saved = owner.get("/api/bookings/" + booking["id"]).json
    assert saved["status"] == "cancelled"
    assert saved["data"].get("paid_at") is None
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid_review"
    state = financial_state(app)
    assert len(state["receipts"]) == len(state["reconciliations"]) == 1
    assert state["receipts"][0]["status"] == "booking_not_confirmed"
    assert state["receipts"][0]["amount"] == 390000
    assert state["fulfillments"] == 0
    assert pay(app, order).status_code == 200
    assert financial_state(app) == state


def test_second_order_for_one_booking_cannot_overwrite_first_payment(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    first = booking_order(owner, booking, tariff, person, "first")
    second = booking_order(owner, booking, tariff, person, "second")
    prepare_lava_payment(app, first)
    prepare_lava_payment(app, second)
    assert pay(app, first).status_code == 200
    original = owner.get("/api/bookings/" + booking["id"]).json
    app.clock[0] += 1
    assert pay(app, second).status_code == 200
    saved = owner.get("/api/bookings/" + booking["id"]).json
    assert saved["data"]["order_id"] == first["id"]
    assert saved["data"]["paid_at"] == original["data"]["paid_at"]
    assert owner.get("/api/orders/" + first["id"]).json["status"] == "paid"
    assert owner.get("/api/orders/" + second["id"]).json["status"] == "paid_review"
    state = financial_state(app)
    assert sorted(receipt["status"] for receipt in state["receipts"]) == [
        "confirmed",
        "duplicate_payment",
    ]
    assert len(state["reconciliations"]) == state["fulfillments"] == 1


def test_cancellation_invalidates_an_unordered_quote(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    quote = booking_quote(owner, booking, tariff, person)
    assert quote.status_code == 201
    owner.post("/api/bookings/" + booking["id"] + "/cancel", json={})
    response = owner.post("/api/orders", json={"quote_id": quote.json["id"], "key": "stale"})
    assert response.status_code == 422, response.json


def test_existing_booking_payment_invalidates_other_quotes_and_new_quotes(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    stale = booking_quote(owner, booking, tariff, person)
    order = booking_order(owner, booking, tariff, person)
    assert pay(app, order).status_code == 200
    response = owner.post("/api/orders", json={"quote_id": stale.json["id"], "key": "stale"})
    assert response.status_code == 422, response.json
    assert booking_quote(owner, booking, tariff, person).status_code == 422
    replay = owner.post(
        "/api/orders", json={"quote_id": order["data"]["quote_id"], "key": "booking-payment"}
    )
    assert replay.status_code == 200
    assert replay.json["id"] == order["id"]


def test_late_booking_money_stays_in_the_ledger_for_review(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    order = booking_order(owner, booking, tariff, person)
    prepare_lava_payment(app, order)
    app.clock[0] = order["data"]["expires"] + 1
    assert pay(app, order).status_code == 200
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid_review"
    assert owner.get("/api/bookings/" + booking["id"]).json["data"].get("paid_at") is None
    state = financial_state(app)
    assert state["receipts"][0]["status"] == "late_payment"
    assert len(state["reconciliations"]) == 1
    assert state["fulfillments"] == 0


def test_delayed_webhook_accepts_an_on_time_payment_without_repricing(app, confirmed_booking):
    _, owner, booking, tariff, person = confirmed_booking
    order = booking_order(owner, booking, tariff, person)
    attempt = prepare_lava_payment(app, order)
    provider_id = attempt["data"]["provider_id"]
    paid_at = order["created"] + 1
    app.clock[0] = order["data"]["expires"] + 60
    upstream = app.config["HTTP_TRANSPORT"]

    def transport(method, url, data, headers):
        if method == "GET" and url == "https://gate.lava.top/api/v1/invoices/" + provider_id:
            return {
                "id": provider_id,
                "status": "completed",
                "datetime": datetime.fromtimestamp(paid_at, timezone.utc).isoformat(),
                "receipt": {"amount": 3900, "currency": "RUB"},
            }
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = transport
    response = app.test_client().post(
        "/api/webhooks/lava",
        headers={
            "X-Api-Key": app.config["LAVA_WEBHOOK_SECRET"],
        },
        json={"eventType": "payment.success", "contractId": provider_id},
    )
    assert response.status_code == 200, response.json
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid"
    assert owner.get("/api/bookings/" + booking["id"]).json["data"]["paid_at"] == paid_at
    state = financial_state(app)
    assert state["receipts"][0]["received_at"] == app.clock[0]
    assert state["receipts"][0]["status"] == "confirmed"
    assert state["reconciliations"] == []


@pytest.fixture
def consultation_app(tmp_path):
    from tests.test_consultation_commerce import app as configured_app

    return configured_app.__wrapped__(tmp_path)


def consultation_events(application):
    with connect(application.config["DATABASE"]) as db:
        return [
            dict(row) for row in db.execute("SELECT * FROM outbox WHERE event='consultation.paid'")
        ]


def test_real_consultation_payment_emits_one_durable_booking_event(consultation_app):
    from tests.test_consultation_commerce import confirmed_consultation, quote

    application = consultation_app
    _, owner, run, booking, _, _, _, _ = confirmed_consultation(application)
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    response = owner.post(
        "/api/orders", json={"quote_id": response.json["id"], "key": "consultation"}
    )
    assert response.status_code == 200, response.json
    order = response.json
    assert order["data"]["consultation_run_id"] == run["id"]
    assert consultation_events(application) == []
    assert pay(application, order).status_code == 200
    assert pay(application, order).status_code == 200
    events = consultation_events(application)
    assert len(events) == 1
    assert events[0]["owner"] == booking["owner"]
    assert events[0]["object_id"] == booking["id"]
    assert events[0]["status"] == "pending"
    assert owner.get("/api/bookings/" + booking["id"]).json["data"]["order_id"] == order["id"]


def test_admin_free_consultation_does_not_emit_payment_event(consultation_app):
    from tests.test_consultation_commerce import confirmed_consultation, confirm_booking, quote
    from tests.test_funnel import finish, start

    application = consultation_app
    admin, _, _, _, _, _, _, _ = confirmed_consultation(application)
    run = finish(admin, start(admin, key="admin-consultation"), ["b"] * 6)
    response = admin.post(
        "/api/funnel-runs/" + run["id"] + "/book", json={"preferences": "Fixture"}
    )
    assert response.status_code == 200, response.json
    booking = confirm_booking(application, admin, admin, response.json)
    response = quote(admin, booking)
    assert response.status_code == 201, response.json
    before = financial_state(application)["receipts"]
    response = admin.post(
        "/api/orders", json={"quote_id": response.json["id"], "key": "admin-consultation"}
    )
    assert response.status_code == 200, response.json
    assert response.json["status"] == "paid"
    assert response.json["data"]["access_source"] == "admin"
    assert consultation_events(application) == []
    assert financial_state(application)["receipts"] == before
