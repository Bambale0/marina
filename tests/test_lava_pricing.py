import pytest

from marina import Problem
from marina.providers import invoice
from tests.test_identity import app


@pytest.mark.parametrize("minor_units", [1, 123, 49000, 99999, 123456789])
def test_lava_invoice_uses_order_snapshot_amount(app, minor_units):
    app.config.update(LAVA_API_KEY="fixture", PUBLIC_URL="https://fixture.test")
    requests = []

    def transport(method, url, data, headers):
        requests.append((method, url, data))
        return {"id": "fixture-invoice", "paymentUrl": "https://app.lava.top/fixture"}

    app.config["HTTP_TRANSPORT"] = transport
    attempt = {
        "id": "attempt",
        "data": {
            "provider": "lava",
            "email": "buyer@example.test",
            "offer_id": "dynamic-offer",
            "currency": "RUB",
            "total": minor_units,
        },
    }
    assert invoice(app, attempt)["provider_id"] == "fixture-invoice"
    assert requests[0][0:2] == ("POST", "https://gate.lava.top/api/v3/invoice")
    payload = requests[0][2]
    assert payload["offerId"] == "dynamic-offer"
    assert payload["currency"] == "RUB"
    assert round(payload["amount"] * 100) == minor_units


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, 9007199254740993, 10**500])
def test_lava_rejects_invalid_snapshot_amount_before_calling_provider(app, amount):
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail("Must not contact provider")
    with pytest.raises(Problem):
        invoice(
            app, {"id": "attempt", "data": {"provider": "lava", "currency": "RUB", "total": amount}}
        )
