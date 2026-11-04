"""A consultation must not invoice the known paid-guide Lava offer."""

import pytest

from tests.test_commerce import product
from tests.test_consultation_commerce import (
    app as consultation_app,
    confirm_booking,
    confirmed_consultation,
    quote,
)
from tests.test_funnel import finish, scenario, start


@pytest.fixture
def app(tmp_path):
    return consultation_app.__wrapped__(tmp_path)


def test_consultation_rejects_the_same_lava_offer_as_its_guide(app):
    admin, owner, run, _, consultation, _, _, _ = confirmed_consultation(app)
    original = admin.get(f"/api/admin/objects/{run['id']}").json["object"]
    guide_id = original["data"]["snapshot"]["guide_product_id"]
    original_guide = admin.get(f"/api/admin/objects/{guide_id}").json["object"]
    guide = product(
        admin,
        "guide",
        amount=49000,
        currency="RUB",
        age_group="3-4",
        material_id=original_guide["data"]["material_id"],
        lava_offer_id=consultation["data"]["lava_offer_id"],
        web_sales_approved=True,
    )
    scenario(
        admin,
        guide_product_id=guide["id"],
        consultation_product_id=consultation["id"],
        contact_url="https://t.me/fixture_support",
    )
    new_run = finish(owner, start(owner, key="same-guide-offer"), ["c"] * 6)
    response = owner.post(
        f"/api/funnel-runs/{new_run['id']}/book", json={"preferences": "Synthetic appointment"}
    )
    assert response.status_code == 200, response.json
    booking = confirm_booking(app, admin, owner, response.json)
    response = quote(owner, booking)
    assert response.status_code == 422, response.json
    assert "offer" in response.json["error"].lower()
