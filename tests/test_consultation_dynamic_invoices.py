"""Each consultation initializes Lava with its own offer and exact server amount."""

import pytest

from tests.test_consultation_commerce import app, confirmed_consultation, CASES, quote
from tests.test_funnel import drain


@pytest.mark.parametrize("group,base,promo", CASES)
@pytest.mark.parametrize("has_coupon", [False, True])
def test_dynamic_consultation_invoice_uses_own_offer_and_exact_price(
    app, group, base, promo, has_coupon
):
    offer_id = "consultation-offer-" + group
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(
        app, group, base, promo, paid_pdf=has_coupon, offer_id=offer_id
    )
    calculation = quote(owner, booking, amount=1, discount=100, offer_id="wrong-offer")
    assert calculation.status_code == 201, calculation.json
    expected_minor = promo * 100 if has_coupon else base
    assert calculation.json["data"]["total"] == expected_minor
    order = owner.post(
        "/api/orders",
        json={"quote_id": calculation.json["id"], "key": "consultation-invoice"},
    )
    assert order.status_code == 200, order.json
    requests = []
    upstream = app.config["HTTP_TRANSPORT"]

    def transport(method, url, data, headers):
        if method == "POST" and url == "https://gate.lava.top/api/v3/invoice":
            requests.append(data)
            return {"id": "consultation-dynamic", "paymentUrl": "https://app.lava.top/test"}
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = transport
    checkout_url = "/api/orders/" + order.json["id"] + "/checkout"
    first = owner.post(checkout_url, json={"email": "buyer@example.test", "amount": 1})
    assert first.status_code == 202, first.json
    drain(app)
    repeated = owner.post(checkout_url, json={"email": "buyer@example.test"})
    assert repeated.status_code == 200, repeated.json
    drain(app)
    assert len(requests) == 1
    assert requests[0]["offerId"] == offer_id
    assert requests[0]["currency"] == "RUB"
    assert requests[0]["amount"] == expected_minor / 100
    assert repeated.json["id"] == first.json["id"]
    assert repeated.json["data"]["total"] == expected_minor
