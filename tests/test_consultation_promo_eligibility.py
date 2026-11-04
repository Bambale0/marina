"""A PDF coupon is reserved only with an entitlement valid at request time."""

import pytest

from tests.test_commerce import pay
from tests.test_consultation_commerce import app as consultation_app, login
from tests.test_funnel import setup_sale, finish, start, drain


@pytest.fixture
def app(tmp_path):
    return consultation_app.__wrapped__(tmp_path)


def delivered_guide(app, admin_access=False):
    admin, owner, run, _, _, sent, files = setup_sale(app)
    if admin_access:
        owner = admin
        run = finish(owner, start(owner, key="admin-promo"), ["b"] * 6)
        app.clock[0] += 30
        drain(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    if not admin_access:
        assert pay(app, order).status_code == 200
    drain(app)
    run = owner.get(f"/api/funnel-runs/{run['id']}").json
    assert run["data"]["guide_delivered_at"]
    right = admin.get(f"/api/admin/objects/{run['data']['guide_right_id']}").json["object"]
    return admin, owner, run, right, order


def book(owner, run):
    response = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "После 18:00 МСК"}
    )
    assert response.status_code == 200, response.json
    return response.json


def revoke(admin, right):
    response = admin.post(
        f"/api/admin/rights/{right['id']}/revoke",
        json={
            "revision": right["revision"],
            "reason": "Synthetic entitlement revoked before coupon reservation",
        },
    )
    assert response.status_code == 200, response.json


def test_active_paid_guide_can_reserve_coupon(app):
    _, owner, run, _, _ = delivered_guide(app)
    assert "TEST_PROMO" in owner.get(f"/api/funnel-runs/{run['id']}/consultation").json["text"]
    assert book(owner, run)["data"]["promotion"]["eligible"] is True


def test_revoked_guide_cannot_advertise_or_reserve_coupon(app):
    admin, owner, run, right, _ = delivered_guide(app)
    revoke(admin, right)
    assert "TEST_PROMO" not in owner.get(f"/api/funnel-runs/{run['id']}/consultation").json["text"]
    assert book(owner, run)["data"]["promotion"]["eligible"] is False


def test_refunded_guide_cannot_reserve_coupon(app):
    _, owner, run, _, order = delivered_guide(app)
    response = app.test_client().post(
        "/api/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": app.config["TELEGRAM_WEBHOOK_SECRET"]},
        json={
            "message": {
                "refunded_payment": {
                    "invoice_payload": order["id"],
                    "telegram_payment_charge_id": "charge-1",
                    "total_amount": order["data"]["total"],
                    "currency": "XTR",
                }
            }
        },
    )
    assert response.status_code == 200
    assert book(owner, run)["data"]["promotion"]["eligible"] is False


@pytest.mark.parametrize("lose_admin", [False, True])
def test_admin_grant_coupon_requires_current_admin_privileges(app, lose_admin):
    _, owner, run, _, _ = delivered_guide(app, admin_access=True)
    if lose_admin:
        app.config["ADMIN_TELEGRAM_IDS"] = set()
        app.clock[0] += 1
        owner = login(app, 1)
        assert owner.get("/api/me").json["account"]["role"] == "client"
    assert book(owner, run)["data"]["promotion"]["eligible"] is (not lose_admin)


@pytest.mark.parametrize("later", ["revoke", "expiry"])
def test_valid_reservation_remains_frozen_after_later_changes(app, later):
    admin, owner, run, right, _ = delivered_guide(app)
    reserved = book(owner, run)
    if later == "revoke":
        revoke(admin, right)
    else:
        app.clock[0] = run["data"]["consultation_expires"] + 1
    assert book(owner, run)["data"]["promotion"] == reserved["data"]["promotion"]
    assert "TEST_PROMO" in owner.get(f"/api/funnel-runs/{run['id']}/consultation").json["text"]
