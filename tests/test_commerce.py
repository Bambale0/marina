import pytest
from datetime import datetime, timezone

from tests.test_identity import app, login, signed
from tests.test_screening import publish, subject


def policy(c):
    return publish(
        c,
        "policy",
        {
            "type": "commerce",
            "family_at": "quote",
            "include_future": True,
            "rank": "first_paid_then_created",
            "rounding": "half_up",
            "trial_purchase": "after_trial",
            "quote_ttl_seconds": 900,
            "renewal_window_hours": 10000,
            "max_future_periods": 2,
            "late_payment": "review",
            "terms": "Fixture policy; not approved for live sales",
        },
    )


def product(c, service="subscription", **extra):
    if service == "case":
        extra.setdefault(
            "case_questions", [{"id": "context", "text": "Описание ситуации", "required": True}]
        )
    return publish(
        c,
        "product",
        {
            "service": service,
            "amount": 10000,
            "currency": "XTR",
            "period_hours": 720,
            "terms": "Synthetic purchase",
            "launch_approved": True,
            **extra,
        },
    )


def quote(c, p, subjects):
    return c.post(
        "/api/quotes",
        json={
            "product_id": p["id"],
            "subject_ids": [s["id"] for s in subjects],
            "surface": "telegram",
        },
    )


def prepare_lava_payment(app, order):
    """Issue a synthetic invoice through public checkout before advancing the clock."""
    from marina.operations import work_once

    for key, value in {
        "LAVA_API_KEY": "fixture-api-key",
        "LAVA_WEBHOOK_SECRET": "fixture-webhook-key",
        "PUBLIC_URL": "https://fixture.test",
    }.items():
        app.config[key] = app.config.get(key) or value
    if "lava_test_clients" not in app.extensions:
        clients = []
        for user_id in (1, 2):
            client = app.test_client()
            response = client.post(
                "/api/auth/telegram",
                json={"init_data": signed(user_id, app.clock[0], query_id="lava-fixture")},
            )
            assert response.status_code == 200, response.json
            client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + response.json["token"]
            clients.append(client)
        app.extensions["lava_test_clients"] = clients
    admin, owner = app.extensions["lava_test_clients"]
    attempts = admin.get("/api/admin/objects?kind=payment_attempt").json["items"]
    existing = next(
        (attempt for attempt in attempts if attempt["data"]["order_id"] == order["id"]), None
    )
    if existing and existing["status"] == "ready":
        return existing
    upstream = app.config.get("HTTP_TRANSPORT")

    def transport(method, url, data, headers):
        if url == "https://gate.lava.top/api/v3/invoice" and method == "POST":
            return {
                "id": "fixture-invoice-" + data["clientUtm"]["utm_source"],
                "paymentUrl": "https://app.lava.top/fixture",
            }
        return upstream(method, url, data, headers) if upstream else {"ok": True, "result": True}

    app.config["HTTP_TRANSPORT"] = transport
    response = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert response.status_code in (200, 202), response.json
    attempt = response.json
    for _ in range(50):
        current = admin.get("/api/admin/objects/" + attempt["id"]).json["object"]
        if current["status"] == "ready":
            return current
        assert work_once(app, send=lambda *args: None) == 1
    pytest.fail("Synthetic Lava invoice did not become ready")


def pay(app, order, charge="charge-1", amount=None):
    if order["data"]["provider"] == "lava":
        attempt = prepare_lava_payment(app, order)
        provider_id = attempt["data"]["provider_id"]
        upstream = app.config.get("HTTP_TRANSPORT")
        paid_at = datetime.fromtimestamp(app.clock[0], timezone.utc).isoformat()

        def transport(method, url, data, headers):
            if method == "GET" and url == "https://gate.lava.top/api/v1/invoices/" + provider_id:
                return {
                    "id": provider_id,
                    "status": "completed",
                    "datetime": paid_at,
                    "receipt": {
                        "amount": (order["data"]["total"] if amount is None else amount) / 100,
                        "currency": "RUB",
                    },
                }
            return upstream(method, url, data, headers)

        app.config["HTTP_TRANSPORT"] = transport
        return app.test_client().post(
            "/api/webhooks/lava",
            headers={"X-Api-Key": app.config["LAVA_WEBHOOK_SECRET"]},
            json={"eventType": "payment.success", "contractId": provider_id},
        )
    c = app.test_client()
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
    return c.post(
        "/api/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        json={
            "update_id": 1,
            "message": {
                "from": {"id": 2},
                "successful_payment": {
                    "invoice_payload": order["id"],
                    "currency": order["data"]["currency"],
                    "total_amount": order["data"]["total"] if amount is None else amount,
                    "telegram_payment_charge_id": charge,
                },
            },
        },
    )


def test_server_price_family_and_immutable_snapshot(app):
    a, c = login(app), login(app, 2)
    policy(a)
    p = product(a)
    children = [subject(c, "child") for _ in range(4)]
    unused = subject(c, "child")
    q = quote(c, p, list(reversed(children))).json
    assert q["data"]["total"] == 38400
    assert [line["discount"] for line in q["data"]["lines"]] == [0, 3, 5, 8]
    order = c.post("/api/orders", json={"quote_id": q["id"], "key": "buy"}).json
    assert c.get("/api/orders/" + order["id"]).json["status"] == "pending"
    assert pay(app, order).status_code == 200
    assert pay(app, order).status_code == 200
    rights = [x for x in c.get("/api/history").json["items"] if x["kind"] == "right"]
    assert len(rights) == 4
    second = quote(c, p, [{"id": q["data"]["family"][-1]}]).json
    assert second["data"]["lines"][0]["discount"] == 8
    assert unused["id"] not in second["data"]["family"]
    assert pay(app, order, "charge-2").status_code == 200
    assert len([x for x in c.get("/api/history").json["items"] if x["kind"] == "right"]) == 4
    assert any(
        x["kind"] == "reconciliation"
        for x in a.get("/api/admin/objects?kind=reconciliation").json["items"]
    )


def test_wrong_amount_never_fulfills_and_secret_required(app):
    a, c = login(app), login(app, 2)
    policy(a)
    p = product(a)
    q = quote(c, p, [subject(c, "child")]).json
    o = c.post("/api/orders", json={"quote_id": q["id"], "key": "buy"}).json
    assert app.test_client().post("/api/webhooks/telegram", json={}).status_code == 503
    assert pay(app, o, amount=1).status_code == 200
    assert c.get("/api/orders/" + o["id"]).json["status"] == "pending"
    assert not [x for x in c.get("/api/history").json["items"] if x["kind"] == "right"]


def test_unconfigured_policy_blocks_subscription_sales(app):
    a, c = login(app), login(app, 2)
    p = product(a)
    assert quote(c, p, [subject(c, "child")]).status_code == 422


def test_two_providers_same_intent_fulfill_once(app):
    from marina.commerce import confirm
    from marina.db import transaction, unpack

    a, c = login(app), login(app, 2)
    policy(a)
    s = subject(c, "child")
    stars = product(a)
    rub = product(a, currency="RUB", web_sales_approved=True)
    intent = c.post(
        "/api/intents",
        json={
            "service": "subscription",
            "subject_ids": [s["id"]],
            "key": "same-purchase",
        },
    ).json
    q1 = c.post(
        "/api/quotes",
        json={
            "product_id": stars["id"],
            "subject_ids": [s["id"]],
            "surface": "telegram",
            "intent_id": intent["id"],
        },
    ).json
    q2 = c.post(
        "/api/quotes",
        json={
            "product_id": rub["id"],
            "subject_ids": [s["id"]],
            "surface": "web",
            "intent_id": intent["id"],
        },
    ).json
    o1 = c.post("/api/orders", json={"quote_id": q1["id"], "key": "stars"}).json
    o2 = c.post("/api/orders", json={"quote_id": q2["id"], "key": "rub"}).json
    assert pay(app, o1).status_code == 200
    with transaction(app.config["DATABASE"]) as db:
        confirm(
            db,
            o2,
            "lava",
            "lava-charge",
            o2["data"]["total"],
            "RUB",
            app.clock[0],
            app.clock[0],
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM intent_fulfillments").fetchone()[0] == 1
    assert len([o for o in c.get("/api/history").json["items"] if o["kind"] == "right"]) == 1


def test_payment_event_survives_interrupted_fulfillment(app, monkeypatch):
    import marina.commerce as commerce
    from marina.db import connect
    from marina.operations import work_once

    a, c = login(app), login(app, 2)
    policy(a)
    p = product(a)
    q = quote(c, p, [subject(c, "child")]).json
    order = c.post("/api/orders", json={"quote_id": q["id"], "key": "crash"}).json
    original = commerce.confirm

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(commerce, "confirm", crash)
    assert pay(app, order).status_code == 200
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT status FROM objects WHERE kind='payment_event'").fetchone()[0]
            == "received"
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    monkeypatch.setattr(commerce, "confirm", original)
    assert work_once(app, send=lambda *args: None) == 1
    assert c.get("/api/orders/" + order["id"]).json["status"] == "paid"
    assert len([x for x in c.get("/api/history").json["items"] if x["kind"] == "right"]) == 1


def test_confirmed_refund_only_revokes_its_own_purchase(app):
    from marina.operations import work_once

    a, c = login(app), login(app, 2)
    publish(a, "policy", {**policy(a)["data"], "refund_policy_approved": True})
    p = product(a)
    children = [subject(c, "child"), subject(c, "child")]
    orders = []
    for i, s in enumerate(children):
        q = quote(c, p, [s]).json
        order = c.post("/api/orders", json={"quote_id": q["id"], "key": "buy-" + str(i)}).json
        pay(app, order, "charge-" + str(i))
        orders.append(c.get("/api/orders/" + order["id"]).json)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    app.config["HTTP_TRANSPORT"] = lambda *args: {"ok": True, "result": True}
    res = a.post(
        "/api/admin/orders/" + orders[0]["id"] + "/refund",
        json={
            "revision": orders[0]["revision"],
            "charge": "charge-0",
            "amount": orders[0]["data"]["total"],
            "reason": "Approved full return",
        },
    )
    assert res.status_code == 202, res.json
    for _ in range(20):
        if not work_once(app, send=lambda *args: None):
            break
    rights = [r for r in c.get("/api/history").json["items"] if r["kind"] == "right"]
    assert len([r for r in rights if r["status"] == "active"]) == 1
    assert len([r for r in rights if r["status"] == "revoked"]) == 1
    assert c.get("/api/orders/" + orders[1]["id"]).json["status"] == "paid"


def test_new_app_defaults_to_lava_only(tmp_path, monkeypatch):
    from marina import create_app

    monkeypatch.delenv("ENABLED_PAYMENT_PROVIDERS", raising=False)
    configured = create_app({"DATABASE": str(tmp_path / "defaults.sqlite"), "TESTING": True})
    assert configured.config["ENABLED_PAYMENT_PROVIDERS"] == ["lava"]


def legacy_stars_order(app):
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava", "stars"]
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(admin)
    q = quote(owner, tariff, [subject(owner, "child")]).json
    order = owner.post("/api/orders", json={"quote_id": q["id"], "key": "legacy"}).json
    return admin, owner, tariff, q, order


def test_lava_only_accepts_ruble_quote_from_telegram_and_creates_lava_invoice(app):
    from marina.operations import work_once

    app.config.update(
        ENABLED_PAYMENT_PROVIDERS=["lava"],
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture-webhook",
        PUBLIC_URL="https://fixture.test",
    )
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(
        admin, currency="RUB", amount=49000, web_sales_approved=True, lava_offer_id="fixture-offer"
    )
    response = quote(owner, tariff, [subject(owner, "child")])
    assert response.status_code == 201, response.json
    assert response.json["data"]["provider"] == "lava"
    order = owner.post(
        "/api/orders", json={"quote_id": response.json["id"], "key": "ruble-checkout"}
    ).json
    requests = []

    def transport(method, url, data, headers):
        requests.append((method, url, data))
        return {"id": "fixture-invoice", "paymentUrl": "https://app.lava.top/fixture"}

    app.config["HTTP_TRANSPORT"] = transport
    checkout = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert checkout.status_code == 202, checkout.json
    assert work_once(app) == 1
    assert len(requests) == 1
    assert requests[0][0:2] == ("POST", "https://gate.lava.top/api/v3/invoice")
    assert requests[0][2]["amount"] == 490
    assert requests[0][2]["currency"] == "RUB"
    assert pay(app, order).status_code == 200
    assert pay(app, order).status_code == 200
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid"
    assert (
        len([item for item in owner.get("/api/history").json["items"] if item["kind"] == "right"])
        == 1
    )


def test_disabled_stars_rejects_new_quotes(app):
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(admin)
    response = quote(owner, tariff, [subject(owner, "child")])
    assert response.status_code == 422


def test_disabled_stars_rejects_new_order_from_saved_quote(app):
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava", "stars"]
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(admin)
    q = quote(owner, tariff, [subject(owner, "child")]).json
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    response = owner.post("/api/orders", json={"quote_id": q["id"], "key": "disabled-order"})
    assert response.status_code == 422


def test_disabled_stars_checkout_does_not_return_an_existing_attempt(app):
    _, owner, _, _, order = legacy_stars_order(app)
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "fixture-webhook"
    first = owner.post("/api/orders/" + order["id"] + "/checkout", json={})
    assert first.status_code == 202
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    repeated = owner.post("/api/orders/" + order["id"] + "/checkout", json={})
    assert repeated.status_code == 422
    assert owner.get("/api/orders/" + order["id"]).status_code == 200


def test_disabled_stars_precheckout_is_rejected_without_losing_later_payment(app):
    _, owner, _, _, order = legacy_stars_order(app)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "fixture-webhook"
    sent = []

    def transport(method, url, data, headers):
        sent.append((url.rsplit("/", 1)[-1], data))
        return {"ok": True, "result": True}

    app.config["HTTP_TRANSPORT"] = transport
    response = app.test_client().post(
        "/api/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "fixture-webhook"},
        json={
            "pre_checkout_query": {
                "id": "old-stars-precheckout",
                "from": {"id": 2},
                "invoice_payload": order["id"],
                "total_amount": order["data"]["total"],
                "currency": "XTR",
            }
        },
    )
    assert response.status_code == 200
    assert sent[-1][0] == "answerPreCheckoutQuery"
    assert sent[-1][1]["ok"] is False
    assert pay(app, order).status_code == 200
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "paid"
    replay = owner.post(
        "/api/orders", json={"quote_id": order["data"]["quote_id"], "key": "legacy"}
    )
    assert replay.status_code == 200
    assert replay.json["id"] == order["id"]


def test_stars_precheckout_cannot_approve_a_lava_order_even_when_stars_enabled(app):
    admin, owner = login(app), login(app, 2)
    policy(admin)
    tariff = product(admin, currency="RUB", web_sales_approved=True)
    q = quote(owner, tariff, [subject(owner, "child")]).json
    order = owner.post("/api/orders", json={"quote_id": q["id"], "key": "ruble"}).json
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "fixture-webhook"
    answers = []

    def transport(method, url, data, headers):
        answers.append(data)
        return {"ok": True, "result": True}

    app.config["HTTP_TRANSPORT"] = transport
    response = app.test_client().post(
        "/api/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "fixture-webhook"},
        json={
            "pre_checkout_query": {
                "id": "forged-precheckout",
                "from": {"id": 2},
                "invoice_payload": order["id"],
                "total_amount": order["data"]["total"],
                "currency": "XTR",
            }
        },
    )
    assert response.status_code == 200
    assert answers[-1]["ok"] is False


def test_historical_stars_refund_event_is_processed_after_disabling_new_invoices(app):
    _, owner, _, _, order = legacy_stars_order(app)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    assert pay(app, order).status_code == 200
    response = app.test_client().post(
        "/api/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        json={
            "message": {
                "from": {"id": 2},
                "refunded_payment": {
                    "invoice_payload": order["id"],
                    "currency": "XTR",
                    "total_amount": order["data"]["total"],
                    "telegram_payment_charge_id": "charge-1",
                },
            }
        },
    )
    assert response.status_code == 200
    assert owner.get("/api/orders/" + order["id"]).json["status"] == "refunded"
    assert not [
        item
        for item in owner.get("/api/history").json["items"]
        if item["kind"] == "right" and item["status"] == "active"
    ]


@pytest.mark.parametrize("provider", ["stars", "prodamus", "yookassa"])
def test_disabled_invoice_provider_never_reaches_network(app, provider):
    from marina import Problem
    from marina.providers import invoice

    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail("Disabled provider reached network")
    with pytest.raises(Problem):
        invoice(app, {"data": {"provider": provider}})
