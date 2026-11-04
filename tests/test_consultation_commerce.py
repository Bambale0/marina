"""Consultation prices and reserved PDF coupons at the public API boundary."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from marina.consultation_commerce import register_consultation_commerce
from marina.db import connect
from tests.funnel_fixtures import configure_funnel_source
from tests.test_commerce import pay, policy, product
from tests.test_funnel import drain, scenario, setup_sale
from tests.test_identity import app as base_app, signed
from tests.test_screening import publish

CASES = [
    ("3-4", 600000, 3900),
    ("5-6", 600000, 3900),
    ("7-9", 650000, 3900),
    ("10-14", 700000, 4500),
]


@pytest.fixture
def app(tmp_path):
    configured = base_app.__wrapped__(tmp_path)
    if "consultation_quote" not in configured.view_functions:
        register_consultation_commerce(configured)
    configure_funnel_source(configured, tmp_path)
    return configured


def login(app, uid=1):
    client = app.test_client()
    response = client.post(
        "/api/auth/telegram",
        json={
            "init_data": signed(uid, app.clock[0], query_id=str(uuid4())),
        },
    )
    assert response.status_code == 200, response.json
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + response.json["token"]
    return client


def pay_consultation(app, owner, order, amount=None):
    upstream = app.config["HTTP_TRANSPORT"]
    provider_id = "consultation-invoice-" + order["id"]

    def transport(method, url, data, headers):
        if method == "POST" and url == "https://gate.lava.top/api/v3/invoice":
            return {"id": provider_id, "paymentUrl": "https://app.lava.top/fixture"}
        if method == "GET" and url == "https://gate.lava.top/api/v1/invoices/" + provider_id:
            return {
                "id": provider_id,
                "status": "completed",
                "datetime": datetime.fromtimestamp(app.clock[0], timezone.utc).isoformat(),
                "receipt": {
                    "amount": (order["data"]["total"] if amount is None else amount) / 100,
                    "currency": "RUB",
                },
            }
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = transport
    if owner.get(f"/api/orders/{order['id']}").json["status"] == "pending":
        checkout = owner.post(
            f"/api/orders/{order['id']}/checkout", json={"email": "buyer@example.test"}
        )
        assert checkout.status_code in (200, 202), checkout.json
        drain(app)
    return app.test_client().post(
        "/api/webhooks/lava",
        headers={"X-Api-Key": "fixture-webhook-key"},
        json={"eventType": "payment.success", "contractId": provider_id},
    )


def confirmed_consultation(
    app,
    group="3-4",
    base=600000,
    promo=3900,
    paid_pdf=True,
    book_after_expiry=False,
    confirm=True,
    legacy_run=False,
    regular_rub=None,
    offer_id="fixture-consultation-offer",
):
    admin = login(app)
    tariff = product(
        admin,
        "booking",
        amount=base,
        currency="RUB",
        age_group=group,
        lava_offer_id=offer_id,
        web_sales_approved=True,
    )
    path = Path(app.config["FUNNEL_SOURCE_PATH"])
    source = json.loads(path.read_text())
    source[group].update(
        consultation_product_id=tariff["id"],
        consultation_amount_rub=promo,
        consultation_regular_rub=base // 100 if regular_rub is None else regular_rub,
        consultation_promo_terms="Synthetic reserved PDF coupon terms.",
        consultation_promo_source={
            "material_id": "fixture-material",
            "pdf_sha256": "a" * 64,
            "page": 13,
        },
    )
    if legacy_run:
        source[group].pop("consultation_product_id")
    path.write_text(json.dumps(source))
    admin, owner, run, _, _, sent, files = setup_sale(app, group=group)
    if legacy_run:
        scenario(admin, group, consultation_product_id=tariff["id"])
    if paid_pdf:
        guide = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
        assert pay(app, guide).status_code == 200
        drain(app)
    if book_after_expiry:
        data = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
        app.clock[0] = data["consultation_expires"] + 1
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Завтра после 18:00 МСК"}
    ).json
    commerce = policy(admin)
    offer = admin.get("/api/admin/objects?kind=template").json["items"][-1]
    published_policy = publish(
        admin,
        "policy",
        {
            **commerce["data"],
            "scope": "consultation",
            "offer_template_id": offer["id"],
        },
    )
    if confirm:
        booking = confirm_booking(app, admin, owner, booking)
    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        LAVA_API_KEY="fixture-api-key",
        LAVA_WEBHOOK_SECRET="fixture-webhook-key",
        PUBLIC_URL="https://fixture.test",
    )
    return admin, owner, run, booking, tariff, published_policy, sent, files


def confirm_booking(app, admin, owner, booking):
    proposed = admin.post(
        f"/api/admin/bookings/{booking['id']}/propose",
        json={
            "revision": booking["revision"],
            "reason": "Synthetic scheduling",
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
        },
    )
    assert proposed.status_code == 200, proposed.json
    confirmed = owner.post(
        f"/api/bookings/{booking['id']}/confirm",
        json={
            "proposal_id": proposed.json["data"]["current_proposal"],
        },
    )
    assert confirmed.status_code == 200, confirmed.json
    return confirmed.json


def quote(owner, booking, **data):
    return owner.post(f"/api/funnel-bookings/{booking['id']}/quote", json=data)


@pytest.mark.parametrize("group,base,promo", CASES)
def test_server_uses_exact_reserved_price_for_each_age(app, group, base, promo):
    _, owner, run, booking, tariff, _, _, _ = confirmed_consultation(app, group, base, promo)
    response = quote(owner, booking, amount=1, discount=99, product_id="untrusted")
    assert response.status_code == 201, response.json
    data = response.json["data"]
    assert data["total"] == promo * 100
    assert data["provider"] == "lava" and data["currency"] == "RUB"
    assert data["product_id"] == tariff["id"] and data["booking_id"] == booking["id"]
    assert data["consultation_run_id"] == run["id"] and "run_id" not in data
    assert data["lines"][0]["subject_id"] == booking["id"]
    assert data["lines"][0]["discount"] == (base - promo * 100) * 100 // base
    assert data["booking_snapshot"]["data"]["promotion"] == booking["data"]["promotion"]
    assert data["promotion_snapshot"] == booking["data"]["promotion"]


def test_reserved_coupon_survives_expiry_before_scheduling_and_payment(app):
    admin, owner, run, booking, _, _, _, _ = confirmed_consultation(app, confirm=False)
    expiry = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]["consultation_expires"]
    app.clock[0] = expiry + 86400
    booking = confirm_booking(app, admin, owner, booking)
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    assert response.json["data"]["total"] == 390000
    order = owner.post("/api/orders", json={"quote_id": response.json["id"], "key": "consultation"})
    assert order.status_code == 200, order.json
    assert pay_consultation(app, owner, order.json).status_code == 200
    assert owner.get(f"/api/bookings/{booking['id']}").json["data"]["paid_at"] == app.clock[0]


@pytest.mark.parametrize("paid_pdf,late", [(False, False), (True, True)])
@pytest.mark.parametrize("group,base,promo", CASES)
def test_unreserved_request_uses_full_price(app, group, base, promo, paid_pdf, late):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(
        app,
        group=group,
        base=base,
        promo=promo,
        paid_pdf=paid_pdf,
        book_after_expiry=late,
    )
    assert booking["data"]["promotion"]["eligible"] is False
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    assert response.json["data"]["total"] == base


def test_quote_requires_confirmed_owned_unpaid_booking(app):
    admin, owner, _, booking, _, _, _, _ = confirmed_consultation(app, confirm=False)
    assert quote(owner, booking).status_code == 422
    assert quote(login(app, 3), booking).status_code == 404
    booking = confirm_booking(app, admin, owner, booking)
    cancelled = owner.post(f"/api/bookings/{booking['id']}/cancel", json={})
    assert cancelled.status_code == 200
    assert quote(owner, booking).status_code == 422


def test_repeated_quote_is_immutable_and_booking_intent_is_unique(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    first = quote(owner, booking)
    assert first.status_code == 201, first.json
    repeated = quote(owner, booking, amount=999999)
    assert repeated.status_code == 200, repeated.json
    assert repeated.json == first.json
    app.clock[0] = first.json["data"]["expires"] + 1
    renewed = quote(owner, booking)
    assert renewed.status_code == 201, renewed.json
    assert renewed.json["id"] != first.json["id"]
    assert renewed.json["data"]["intent_id"] == first.json["data"]["intent_id"]
    assert renewed.json["data"]["total"] == 390000
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM objects WHERE kind='purchase_intent' AND json_extract(data,'$.reference')=?",
                (booking["id"],),
            ).fetchone()[0]
            == 1
        )


def test_lava_receipt_fulfills_booking_once_and_rejects_wrong_amount(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    order = owner.post(
        "/api/orders", json={"quote_id": response.json["id"], "key": "consultation"}
    ).json
    assert pay_consultation(app, owner, order, amount=1).status_code == 200
    assert not owner.get(f"/api/bookings/{booking['id']}").json["data"].get("paid_at")


def test_successful_payment_blocks_another_consultation_quote(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    order = owner.post(
        "/api/orders", json={"quote_id": response.json["id"], "key": "consultation"}
    ).json
    assert pay_consultation(app, owner, order).status_code == 200
    assert pay_consultation(app, owner, order).status_code == 200
    assert quote(owner, booking).status_code == 409
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM fulfillments WHERE order_id=?", (order["id"],)
            ).fetchone()[0]
            == 1
        )


def test_disabled_lava_and_paused_operations_block_quotes(app):
    admin, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = []
    assert quote(owner, booking).status_code == 422
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    publish(
        admin, "policy", {"type": "operations", "terms": "Synthetic pause", "pause_sales": True}
    )
    assert quote(owner, booking).status_code == 422


def test_legacy_run_resolves_only_product_from_current_age_funnel(app):
    admin, owner, run, booking, tariff, _, _, _ = confirmed_consultation(app, legacy_run=True)
    assert (
        not admin.get(f"/api/admin/objects/{run['id']}")
        .json["object"]["data"]["snapshot"]
        .get("consultation_product_id")
    )
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    assert response.json["data"]["product_id"] == tariff["id"]
    assert response.json["data"]["total"] == 390000


def test_full_price_preserves_regular_amount_from_booking_snapshot(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(
        app,
        base=650000,
        regular_rub=6000,
        paid_pdf=False,
    )
    response = quote(owner, booking)
    assert response.status_code == 201, response.json
    assert response.json["data"]["total"] == 600000


def test_rejects_product_for_a_different_age_even_for_legacy_run(app):
    admin, owner, _, booking, _, _, _, _ = confirmed_consultation(app, legacy_run=True)
    wrong_age = product(
        admin,
        "booking",
        amount=700000,
        currency="RUB",
        age_group="10-14",
        lava_offer_id="fixture",
        web_sales_approved=True,
    )
    scenario(admin, "3-4", consultation_product_id=wrong_age["id"])
    assert quote(owner, booking).status_code == 422
