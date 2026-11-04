"""Owner's release choices at public API and external transport boundaries."""

import pytest
from marina.db import connect
from marina.operations import work_once
from tests.test_identity import app, login
from tests.test_funnel import private_source, setup_sale, capture, drain, start, finish
from tests.test_commerce import policy, product, quote
from tests.test_screening import subject
from marina.bot import Bot


def test_disabling_stars_cancels_a_queued_invoice_before_network(app):
    _, owner, run, _, _, _, _ = setup_sale(app)
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "fixture"
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    attempt = owner.post(f"/api/orders/{order['id']}/checkout", json={}).json
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda *args: calls.append(args)
    assert work_once(app) == 1
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT status FROM objects WHERE id=?", (attempt["id"],)).fetchone()[0]
            == "cancelled"
        )
        assert not db.execute("SELECT 1 FROM objects WHERE kind='reconciliation'").fetchone()
    assert not calls


def test_disabled_stars_guide_is_not_offered_or_sold(app):
    _, owner, _, _, _, sent, _ = setup_sale(app)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    run = finish(owner, start(owner, key="disabled-stars"), ["b"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    marker = len(sent)
    app.clock[0] += 180
    drain(app)
    assert len(sent) == marker
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code in (403, 422)
    assert not any(
        p["data"]["currency"] == "XTR" for p in owner.get("/api/catalog/product").json["items"]
    )


def test_lava_only_does_not_require_a_development_flag(app):
    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        DEV_EXTERNAL_LAVA_CHECKOUT=False,
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture",
        PUBLIC_URL="https://fixture.test",
    )
    _, owner, run, _, _, _, _ = setup_sale(
        app, currency="RUB", web_sales_approved=True, lava_offer_id="fixture-offer"
    )
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 200, response.json
    assert response.json["data"]["provider"] == "lava"


@pytest.mark.parametrize("surface", ["telegram", "web"])
def test_public_guide_quote_uses_the_configured_lava_route(app, surface):
    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture",
        PUBLIC_URL="https://fixture.test",
    )
    _, owner, run, config, _, _, _ = setup_sale(
        app, currency="RUB", web_sales_approved=True, lava_offer_id="fixture-offer"
    )
    response = owner.post(
        "/api/quotes",
        json={
            "product_id": config["data"]["guide_product_id"],
            "run_id": run["id"],
            "surface": surface,
        },
    )
    assert response.status_code == 201, response.json
    assert response.json["data"]["provider"] == "lava"


def test_bot_delivers_generic_lava_invoice_after_email(app):
    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture",
        PUBLIC_URL="https://fixture.test",
    )
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(
        admin, currency="RUB", amount=49000, web_sales_approved=True, lava_offer_id="fixture"
    )
    calculation = quote(owner, tariff, [subject(owner, "child")]).json
    calls = []

    def transport(method, url, data, headers):
        calls.append((url.rsplit("/", 1)[-1], data))
        if url.endswith("/invoice"):
            return {"id": "fixture-invoice", "paymentUrl": "https://app.lava.top/fixture"}
        return {"ok": True, "result": {"message_id": 1}}

    app.config["HTTP_TRANSPORT"] = transport
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "generic-lava")
    bot.action("buy", {"id": calculation["id"]})
    bot.message({"text": "buyer@example.test"})
    drain(app)
    messages = [data for endpoint, data in calls if endpoint == "sendMessage"]
    assert (
        messages[-1]["reply_markup"]["inline_keyboard"][0][0]["url"]
        == "https://app.lava.top/fixture"
    )
    assert sum(endpoint == "invoice" for endpoint, _ in calls) == 1
    assert not any(endpoint in ("sendInvoice", "createInvoiceLink") for endpoint, _ in calls)
    before = len(calls)
    drain(app)
    assert len(calls) == before
