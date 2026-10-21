from tests.test_identity import app, login
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


def pay(app, order, charge="charge-1", amount=None):
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
