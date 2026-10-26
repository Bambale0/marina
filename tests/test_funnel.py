import copy

import pytest

from marina.bot import handle_update
from marina.db import connect
from marina.operations import tick, work_once
from tests.test_commerce import pay, policy, product
from tests.test_identity import app, login
from tests.test_screening import publish
from tests.funnel_fixtures import configure_funnel_source


@pytest.fixture(autouse=True)
def private_source(app, tmp_path):
    configure_funnel_source(app, tmp_path)


def scenario(admin, group="3-4", **overrides):
    response = admin.get("/api/admin/funnel-defaults/" + group)
    assert response.status_code == 200, response.json
    return publish(admin, "funnel", {**response.json, **overrides})


def start(client, group="3-4", key="one"):
    response = client.post("/api/funnel-runs", json={"age_group": group, "key": key})
    assert response.status_code == 200, response.json
    return response.json


def finish(client, run, values):
    for index, value in enumerate(values, 1):
        response = client.post(
            f"/api/funnel-runs/{run['id']}/answer",
            json={"revision": run["revision"], "question": f"q{index}", "value": value},
        )
        assert response.status_code == 200, response.json
        run = response.json
    return run


def test_result_waits_30_seconds_and_edits_one_progress_message(app):
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    calls = []

    def transport(method, url, data, headers):
        telegram_method = url.rsplit("/", 1)[-1]
        calls.append((telegram_method, data))
        return {"ok": True, "result": {"message_id": 77}}

    app.config["HTTP_TRANSPORT"] = transport
    run = finish(owner, start(owner), ["b"] * 6)
    completed_at = run["data"]["completed_at"]
    with connect(app.config["DATABASE"]) as db:
        events = db.execute(
            "SELECT event,available FROM outbox WHERE object_id=? ORDER BY available,event",
            (run["id"],),
        ).fetchall()
    assert [(row["event"], row["available"] - completed_at) for row in events] == [
        ("funnel.progress.0", 0),
        ("funnel.progress.1", 5),
        ("funnel.progress.2", 10),
        ("funnel.progress.3", 15),
        ("funnel.progress.4", 20),
        ("funnel.progress.5", 25),
        ("funnel.result", 30),
    ]

    drain(app)
    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": 2,
                "text": "Обрабатываю ваши ответы и готовлю результат…\n\n⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%",
            },
        )
    ]
    visible = owner.get("/api/funnel-runs/" + run["id"]).json
    assert "progress_message_id" not in visible["data"]
    assert "progress_percent" not in visible["data"]
    for percent in (20, 40, 60, 80, 100):
        app.clock[0] += 5
        drain(app)
        assert calls[-1][0] == "editMessageText"
        assert calls[-1][1]["message_id"] == 77
        assert calls[-1][1]["text"].endswith(
            "🟩" * (percent // 10) + "⬜" * (10 - percent // 10) + f" {percent}%"
        )
    app.clock[0] += 4
    drain(app)
    assert not any("ИТОГ СКРИНИНГА" in call[1].get("text", "") for call in calls)
    app.clock[0] += 1
    drain(app)
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["message_id"] == 77
    assert "ИТОГ СКРИНИНГА" in calls[-1][1]["text"]
    assert len([call for call in calls if call[0] == "sendMessage"]) == 1


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
@pytest.mark.parametrize(
    "values,total,zone",
    [
        (["a"] * 6, 0, "green"),
        (["b"] * 3 + ["a"] * 3, 3, "green"),
        (["b"] * 4 + ["a"] * 2, 4, "yellow"),
        (["c"] + ["b"] * 5, 7, "yellow"),
        (["c"] * 4 + ["a"] * 2, 8, "red"),
        (["c"] * 6, 12, "red"),
    ],
)
def test_age_questionnaires_and_exact_score_boundaries(app, group, values, total, zone):
    admin, owner, stranger = login(app), login(app, 2), login(app, 3)
    scenario(admin, group)
    run = start(owner, group)
    assert (
        owner.post("/api/funnel-runs", json={"age_group": group, "key": "one"}).json["id"]
        == run["id"]
    )
    assert stranger.get("/api/funnel-runs/" + run["id"]).status_code == 404
    assert run["data"]["question"]["text"].startswith("[Вопрос 1 из 6]")
    assert "snapshot" not in run["data"]
    run = finish(owner, run, values)
    assert run["status"] == "completed"
    assert run["data"]["result"]["score"] == total
    assert run["data"]["result"]["zone"] == zone
    assert str(total) + " из 12" in run["data"]["result"]["text"]
    assert owner.get("/api/me").json["subjects"] == []
    assert "guide_text" not in str(owner.get("/api/data-export").json)


def approve_existing(admin, obj):
    for action in ("approve", "publish"):
        response = admin.post(
            f"/api/admin/content/{obj['id']}/{action}",
            json={"revision": obj["revision"], "reason": "Synthetic acceptance fixture"},
        )
        assert response.status_code == 200, response.json
        obj = response.json
    return obj


def drain(app):
    for _ in range(100):
        if not work_once(app):
            return
    raise AssertionError("Worker failed to drain the due outbox")


def capture(app):
    sent, files = [], []

    def transport(method, url, data, headers):
        sent.append((url.rsplit("/", 1)[-1], data))
        return {
            "ok": True,
            "result": "https://t.me/$synthetic-invoice"
            if url.endswith("/createInvoiceLink")
            else {"message_id": len(sent)},
        }

    app.config["HTTP_TRANSPORT"] = transport
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    return sent, files


def setup_sale(app, group="3-4", currency="XTR", **product_fields):
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
    admin, owner = login(app), login(app, 2)
    offer = publish(
        admin,
        "template",
        {"type": "offer", "text": "Synthetic owner-supplied offer"},
    )
    publish(
        admin,
        "policy",
        {
            "type": "commerce",
            "scope": "guide",
            "offer_template_id": offer["id"],
            "quote_ttl_seconds": 900,
            "late_payment": "review",
        },
    )
    base = scenario(admin, group)
    response = admin.post(
        f"/api/admin/funnels/{base['id']}/prepare-guide",
        json={"revision": base["revision"], "reason": "Prepare source PDF for review"},
    )
    assert response.status_code == 201, response.json
    material = approve_existing(admin, response.json)
    p = product(
        admin,
        "guide",
        amount=123,
        currency=currency,
        material_id=material["id"],
        age_group=group,
        **product_fields,
    )
    config = scenario(
        admin, group, guide_product_id=p["id"], contact_url="https://t.me/synthetic_fixture"
    )
    sent, files = capture(app)
    run = finish(owner, start(owner, group), ["b"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    assert "ИТОГ СКРИНИНГА" in sent[-1][1]["text"]
    app.clock[0] += 2
    drain(app)
    assert ("123 ⭐" if currency == "XTR" else "1,23 ₽") in sent[-1][1]["text"]
    run = owner.get("/api/funnel-runs/" + run["id"]).json
    return admin, owner, run, config, material, sent, files


def test_dev_tripwire_without_price_can_be_accepted_and_delivers_pdf(app):
    app.config["DEV_TRIPWIRE_BYPASS_PAYMENT"] = True
    admin, owner = login(app), login(app, 2)
    config = scenario(admin)
    assert not config["data"]["guide_product_id"]
    sent, files = capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    app.clock[0] += 2
    drain(app)

    assert "будет указана позже" in sent[-1][1]["text"]
    assert [
        button["text"] for row in sent[-1][1]["reply_markup"]["inline_keyboard"] for button in row
    ] == ["✅ Принять", "❌ Отменить"]
    accepted = owner.post(f"/api/funnel-runs/{run['id']}/dev-tripwire-accept", json={})
    assert accepted.status_code == 200, accepted.json
    assert accepted.json["data"]["dev_tripwire_accepted_at"]
    assert owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={}).status_code == 422
    drain(app)
    assert len(files) == 1 and files[0][2].startswith(b"%PDF")
    assert "Оплата не выполнялась" in sent[-1][1]["text"]
    consultation = owner.get(f"/api/funnel-runs/{run['id']}/consultation").json["text"]
    assert "TEST_PROMO" not in consultation
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Завтра, 18:00 МСК"}
    )
    assert booking.status_code == 200, booking.json
    assert booking.json["data"]["promotion"]["eligible"] is False
    assert (
        owner.post(f"/api/funnel-runs/{run['id']}/dev-tripwire-accept", json={}).status_code == 200
    )
    drain(app)
    assert len(files) == 1
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='order'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM objects WHERE kind='right'").fetchone()[0] == 0


def test_telegram_dev_tripwire_accept_button_continues_without_payment(app):
    app.config["DEV_TRIPWIRE_BYPASS_PAYMENT"] = True
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Consent before dev tripwire",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    sent, files = capture(app)
    finish(owner, start(owner), ["b"] * 6)
    drain(app)
    app.clock[0] += 32
    drain(app)
    app.clock[0] += 2
    drain(app)
    button = next(
        button
        for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
        for button in row
        if button["text"] == "✅ Принять"
    )
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Renewed consent before dev tripwire",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    handle_update(
        app,
        {
            "update_id": 850,
            "callback_query": {
                "id": "dev-tripwire",
                "from": {"id": 2, "first_name": "Мама"},
                "data": button["callback_data"],
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    assert sent[-1][1]["text"] == "Renewed consent before dev tripwire"
    consent = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    handle_update(
        app,
        {
            "update_id": 851,
            "callback_query": {
                "id": "dev-tripwire-consent",
                "from": {"id": 2, "first_name": "Мама"},
                "data": consent,
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    assert "без списания оплаты" in sent[-1][1]["text"]
    drain(app)
    assert len(files) == 1
    assert "Оплата не выполнялась" in sent[-1][1]["text"]


def test_dev_tripwire_without_explicit_bypass_stays_closed(app):
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    sent, _ = capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    drain(app)
    app.clock[0] += 32
    drain(app)
    assert not any("✅ Принять" in str(data) for _, data in sent)
    assert (
        owner.post(f"/api/funnel-runs/{run['id']}/dev-tripwire-accept", json={}).status_code == 404
    )


def test_lava_tripwire_checkout_verifies_payment_and_delivers_pdf(app):
    app.config.update(
        DEV_TRIPWIRE_BYPASS_PAYMENT=True,
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="test-api-key",
        LAVA_WEBHOOK_SECRET="test-webhook-key",
        PUBLIC_URL="https://dev.example.test",
    )
    admin, owner, run, _, _, sent, files = setup_sale(
        app,
        currency="RUB",
        lava_offer_id="836b9fc5-7ae9-4a27-9642-592bc44072b7",
        web_sales_approved=True,
    )
    assert (
        owner.post(f"/api/funnel-runs/{run['id']}/dev-tripwire-accept", json={}).status_code == 422
    )
    order_response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert order_response.status_code == 200, order_response.json
    order = order_response.json
    assert order["data"]["provider"] == "lava"
    assert order["data"]["currency"] == "RUB"
    assert owner.post(f"/api/orders/{order['id']}/checkout", json={}).status_code == 422

    upstream = app.config["HTTP_TRANSPORT"]
    lava_calls = []

    def transport(method, url, data, headers):
        if url.startswith("https://gate.lava.top"):
            lava_calls.append((method, url, data, headers))
            if method == "POST":
                return {
                    "id": "7ea82675-4ded-4133-95a7-a6efbaf165cc",
                    "status": "new",
                    "paymentUrl": "https://app.lava.top/payment/test-contract",
                }
            return {
                "id": "7ea82675-4ded-4133-95a7-a6efbaf165cc",
                "status": "completed",
                "datetime": "2027-01-15T08:00:00Z",
                "receipt": {"amount": 1.23, "currency": "RUB"},
            }
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = transport
    checkout = owner.post(
        f"/api/orders/{order['id']}/checkout", json={"email": "buyer@example.test"}
    )
    assert checkout.status_code == 202, checkout.json
    attempt = checkout.json
    assert work_once(app) == 1
    attempt = admin.get("/api/admin/objects/" + attempt["id"]).json["object"]
    assert attempt["status"] == "ready"
    assert attempt["data"]["url"] == "https://app.lava.top/payment/test-contract"
    drain(app)
    assert any("Lava.top" in data.get("text", "") for _, data in sent)
    request = lava_calls[0]
    assert request[0:2] == ("POST", "https://gate.lava.top/api/v3/invoice")
    assert request[2] == {
        "email": "buyer@example.test",
        "offerId": "836b9fc5-7ae9-4a27-9642-592bc44072b7",
        "currency": "RUB",
        "amount": 1.23,
        "periodicity": "ONE_TIME",
        "clientUtm": {"utm_source": attempt["id"]},
        "successful_return_url": "https://dev.example.test/?payment=success",
        "failure_return_url": "https://dev.example.test/?payment=failed",
        "cancel_return_url": "https://dev.example.test/?payment=cancelled",
    }
    webhook = {
        "eventType": "payment.success",
        "contractId": "7ea82675-4ded-4133-95a7-a6efbaf165cc",
        "amount": 1.23,
        "currency": "RUB",
        "timestamp": "2027-01-15T08:00:00Z",
        "status": "completed",
    }
    assert app.test_client().post("/api/webhooks/lava", json=webhook).status_code == 401
    response = app.test_client().post(
        "/api/webhooks/lava", headers={"X-Api-Key": "test-webhook-key"}, json=webhook
    )
    assert response.status_code == 200, response.json
    drain(app)
    assert owner.get("/api/funnel-runs/" + run["id"]).json["data"]["guide_delivered_at"]
    assert len(files) == 1 and files[0][2].startswith(b"%PDF")
    assert any(call[0] == "GET" and call[1].endswith(webhook["contractId"]) for call in lava_calls)
    repeated = app.test_client().post(
        "/api/webhooks/lava", headers={"X-Api-Key": "test-webhook-key"}, json=webhook
    )
    assert repeated.status_code == 200, repeated.json
    drain(app)
    assert len(files) == 1


def test_telegram_lava_tripwire_collects_email_before_checkout(app):
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="test-api-key",
        LAVA_WEBHOOK_SECRET="test-webhook-key",
        PUBLIC_URL="https://dev.example.test",
    )
    admin, owner, run, _, _, sent, _ = setup_sale(
        app,
        currency="RUB",
        lava_offer_id="836b9fc5-7ae9-4a27-9642-592bc44072b7",
        web_sales_approved=True,
    )
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Consent before checkout",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    upstream = app.config["HTTP_TRANSPORT"]

    def transport(method, url, data, headers):
        if url == "https://gate.lava.top/api/v3/invoice":
            return {
                "id": "7ea82675-4ded-4133-95a7-a6efbaf165cc",
                "status": "new",
                "paymentUrl": "https://app.lava.top/payment/test-contract",
            }
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = transport
    counter = iter(range(700, 800))

    def click(title_prefix):
        button = next(
            button
            for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
            for button in row
            if button["text"].startswith(title_prefix)
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "fixture",
                    "from": {"id": 2, "first_name": "Мама"},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    click("💳 Забрать руководство")
    assert "email" in sent[-1][1]["text"]
    handle_update(
        app,
        {
            "update_id": next(counter),
            "message": {
                "from": {"id": 2, "first_name": "Мама"},
                "chat": {"id": 2, "type": "private"},
                "text": "buyer@example.test",
            },
        },
    )
    assert "Готовлю счёт" in sent[-1][1]["text"]
    drain(app)
    assert "Lava.top" in sent[-1][1]["text"]
    assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"].startswith(
        "https://app.lava.top/"
    )
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    assert current["data"]["order_status"] == "pending"


def test_lava_adjustment_webhook_preserves_envelope_once(app):
    app.config["LAVA_WEBHOOK_SECRET"] = "test-webhook-key"
    payload = {
        "event_id": "7ea82675-4ded-4133-95a7-a6efbaf165cc",
        "event_type": "refund.success",
        "created_at": "2027-01-15T08:00:00Z",
        "data": {
            "refund_id": "2f36c6c0-dd4c-47e0-8759-8219869f0123",
            "refund_type": "full",
            "amount": 1.23,
            "currency": "RUB",
        },
    }
    client = app.test_client()
    for _ in range(2):
        response = client.post(
            "/api/webhooks/lava",
            headers={"X-Api-Key": "test-webhook-key"},
            json=payload,
        )
        assert response.status_code == 200, response.json
    with connect(app.config["DATABASE"]) as db:
        rows = db.execute(
            "SELECT data FROM objects WHERE kind='reconciliation' AND status='open'"
        ).fetchall()
    assert len(rows) == 1
    assert "refund.success" in rows[0]["data"]


def test_external_lava_tripwire_is_disabled_without_explicit_dev_gate(app):
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="test-api-key",
        LAVA_WEBHOOK_SECRET="test-webhook-key",
        PUBLIC_URL="https://dev.example.test",
    )
    _, owner, run, _, _, _, _ = setup_sale(
        app,
        currency="RUB",
        lava_offer_id="836b9fc5-7ae9-4a27-9642-592bc44072b7",
        web_sales_approved=True,
    )
    app.config["DEV_EXTERNAL_LAVA_CHECKOUT"] = False
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 422
    assert "не разрешена" in response.json["error"]


def test_incomplete_lava_setup_does_not_create_tripwire_order(app):
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="test-api-key",
        LAVA_WEBHOOK_SECRET="test-webhook-key",
        PUBLIC_URL="https://dev.example.test",
    )
    _, owner, run, _, _, sent, _ = setup_sale(
        app,
        currency="RUB",
        lava_offer_id="836b9fc5-7ae9-4a27-9642-592bc44072b7",
        web_sales_approved=True,
    )
    app.config["LAVA_API_KEY"] = ""
    marker = len(sent)
    finish(owner, start(owner, key="incomplete-lava"), ["b"] * 6)
    drain(app)
    app.clock[0] += 32
    drain(app)
    assert not any("1,23 ₽" in data.get("text", "") for _, data in sent[marker:])
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 422
    assert "не разрешена" in response.json["error"]


def test_admin_renders_text_material_and_lists_tripwire_entities(app):
    admin, owner = login(app), login(app, 2)
    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Базовый PDF трипваера",
            "data": {
                "owner": "Специалист",
                "status": "received",
                "source": "Тест механики по поручению владельца",
                "rights": "Внутренний тест",
                "format": "pdf",
                "needs_video": False,
                "text": "Базовый текст для проверки выдачи PDF после оплаты.",
                "filename": "tripwire-test.pdf",
            },
            "reason": "Подготовить тестовую выдачу",
        },
    ).json
    rendered = admin.post(
        f"/api/admin/materials/{draft['id']}/render-pdf",
        json={"revision": draft["revision"], "reason": "Сформировать тестовый PDF"},
    )
    assert rendered.status_code == 200, rendered.json
    material = rendered.json
    assert material["data"]["asset_id"]
    assert material["data"]["render_source_sha256"]
    pdf = admin.get(f"/api/admin/materials/{material['id']}/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")

    changed = admin.patch(
        f"/api/admin/content/{material['id']}",
        json={
            "revision": material["revision"],
            "title": material["data"]["title"],
            "data": {**material["data"], "text": "Исправленный базовый текст."},
            "reason": "Проверить сброс устаревшего PDF",
        },
    )
    assert changed.status_code == 200, changed.json
    changed = changed.json
    assert "asset_id" not in changed["data"]
    assert "render_source_sha256" not in changed["data"]

    assert owner.get("/api/admin/tripwire").status_code == 403
    dashboard = admin.get("/api/admin/tripwire")
    assert dashboard.status_code == 200, dashboard.json
    assert dashboard.json["integrations"] == {
        "lava_api": False,
        "lava_webhook": False,
        "public_https": False,
        "external_lava_test_mode": False,
        "tripwire_payment_bypass": False,
    }
    assert dashboard.json["entities"]["material"]["draft"] == 1


def test_admin_manages_offer_and_guide_purchase_policy_without_subscription_rules(app):
    admin = login(app)
    offer = publish(
        admin,
        "template",
        {"type": "offer", "text": "Published owner-supplied offer"},
    )
    guide_policy = publish(
        admin,
        "policy",
        {
            "type": "commerce",
            "scope": "guide",
            "offer_template_id": offer["id"],
            "quote_ttl_seconds": 7200,
            "late_payment": "review",
        },
    )
    dashboard = admin.get("/api/admin/tripwire")
    assert dashboard.status_code == 200, dashboard.json
    assert dashboard.json["offer"]["id"] == offer["id"]
    assert dashboard.json["policy"]["id"] == guide_policy["id"]

    owner = login(app, 2)
    base = scenario(admin)
    material = approve_existing(
        admin,
        admin.post(
            f"/api/admin/funnels/{base['id']}/prepare-guide",
            json={"revision": base["revision"], "reason": "Prepare guide policy test"},
        ).json,
    )
    guide = product(
        admin,
        "guide",
        amount=49000,
        currency="XTR",
        material_id=material["id"],
        age_group="3-4",
    )
    scenario(
        admin,
        guide_product_id=guide["id"],
        contact_url="https://t.me/synthetic_fixture",
    )
    capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    drain(app)
    app.clock[0] += 32
    drain(app)
    app.clock[0] += 2
    drain(app)
    run = owner.get("/api/funnel-runs/" + run["id"]).json
    quote = owner.post(
        f"/api/funnel-runs/{run['id']}/purchase",
        json={"surface": "telegram"},
    )
    assert quote.status_code == 200, quote.json
    assert quote.json["data"]["policy_id"] == guide_policy["id"]
    assert quote.json["data"]["total"] == 49000


def test_consent_unpublished_copy_and_version_snapshots(app):
    admin, owner = login(app), login(app, 2)
    assert owner.get("/api/admin/funnel-defaults/3-4").status_code == 403
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "missing"}).status_code
        == 422
    )
    content = scenario(admin)
    app.config["ENFORCE_CONSENT"] = True
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "no-consent"}).status_code
        == 422
    )
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Separate privacy consent",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "not-accepted"}).status_code
        == 422
    )
    owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    run = start(owner)
    snapshot = copy.deepcopy(content["data"])
    snapshot["method"]["questions"][0]["text"] = "[Вопрос 1 из 6] Новая редакция"
    scenario(admin, **snapshot)
    assert (
        "Новая редакция"
        not in owner.get("/api/funnel-runs/" + run["id"]).json["data"]["question"]["text"]
    )
    newer = start(owner, key="newer")
    assert "Новая редакция" in newer["data"]["question"]["text"]
    assert (
        owner.post(
            f"/api/funnel-runs/{run['id']}/answer",
            json={"revision": run["revision"], "question": "q1", "value": "a"},
        ).status_code
        == 409
    )
    assert "guide_pages" not in str(owner.get("/api/data-export").json)


def test_question_sequence_stale_answers_and_resume(app):
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    run = start(owner)
    path = f"/api/funnel-runs/{run['id']}/answer"
    assert (
        owner.post(
            path, json={"revision": run["revision"], "question": "q6", "value": "c"}
        ).status_code
        == 422
    )
    command = {"revision": run["revision"], "question": "q1", "value": "b"}
    first = owner.post(path, json=command).json
    assert owner.post(path, json=command).json == first
    assert owner.post(path, json={**command, "value": "c"}).status_code == 409
    assert owner.get("/api/funnel-runs/" + run["id"]).json["data"]["question"]["id"] == "q2"
    assert (
        owner.post(
            path, json={"revision": first["revision"], "question": "q2", "value": "bad"}
        ).status_code
        == 422
    )
    sent, _ = capture(app)
    owner.post("/api/funnel-entries", json={"key": "new-start-before-resume"})
    resumed = owner.post(f"/api/funnel-runs/{run['id']}/resume", json={})
    assert resumed.json["data"]["question"]["id"] == "q2"
    app.clock[0] += 1200
    drain(app)
    assert not sent


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
def test_telegram_start_age_six_answers_and_result(app, group):
    from marina.funnel import AGES

    admin = login(app)
    scenario(admin, group)
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Accept before screening",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    app.config["ENFORCE_CONSENT"] = True
    sent, _ = capture(app)
    counter = iter(range(300, 600))

    def message(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {
                    "from": {"id": 2, "first_name": "Мама"},
                    "chat": {"id": 2, "type": "private"},
                    "text": text,
                },
            },
        )

    def click(title):
        button = next(
            b
            for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
            for b in row
            if b["text"] == title
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "fixture",
                    "from": {"id": 2, "first_name": "Мама"},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    message("/start")
    assert "Здравствуйте, Мама" in sent[-1][1]["text"]
    assert len(sent[-1][1]["reply_markup"]["inline_keyboard"]) == 5
    click(AGES[group])
    assert sent[-1][1]["text"] == privacy["data"]["terms"]
    click("Прочитал(а) и принимаю")
    for number in range(1, 7):
        assert f"[Вопрос {number} из 6]" in sent[-1][1]["text"]
        rows = sent[-1][1]["reply_markup"]["inline_keyboard"]
        assert len(rows) == 1
        assert [button["text"] for button in rows[0]] == ["А", "Б", "В"]
        click("Б")
    drain(app)
    assert "Обрабатываю" in sent[-1][1]["text"]
    app.clock[0] += 30
    tick(app)
    drain(app)
    assert "6 из 12" in sent[-1][1]["text"]
    assert "диагноз" in sent[-1][1]["text"]


def test_age_reminder_survives_restart_and_is_cancelled_on_selection(app):
    owner = login(app, 2)
    sent, _ = capture(app)
    entry = owner.post("/api/funnel-entries", json={"key": "first"}).json
    assert owner.post("/api/funnel-entries", json={"key": "first"}).json["id"] == entry["id"]
    app.clock[0] += 1199
    drain(app)
    assert not sent
    app.clock[0] += 1
    drain(app)
    assert "отвлеклись" in sent[-1][1]["text"]
    count = len(sent)
    tick(app)
    drain(app)
    assert len(sent) == count
    second = owner.post("/api/funnel-entries", json={"key": "second"}).json
    owner.post(f"/api/funnel-entries/{second['id']}/age", json={"age_group": "outside"})
    app.clock[0] += 1200
    drain(app)
    assert len(sent) == count


def test_guide_payment_delivery_idempotency_and_nurture(app):
    admin, owner, run, config, material, sent, files = setup_sale(app)
    assert owner.get("/api/admin/materials/" + material["id"] + "/pdf").status_code == 403
    assert admin.get("/api/admin/materials/" + material["id"] + "/pdf").data.startswith(b"%PDF")
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 200, response.json
    order = response.json
    assert owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json["id"] == order["id"]
    assert order["data"]["total"] == 123
    assert order["data"]["currency"] == "XTR"
    attempt = owner.post(f"/api/orders/{order['id']}/checkout", json={}).json
    assert attempt["status"] == "queued"
    drain(app)
    assert (
        sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"]
        == "https://t.me/$synthetic-invoice"
    )
    assert not files
    assert pay(app, order).status_code == 200
    assert pay(app, order).status_code == 200
    drain(app)
    assert len(files) == 1 and files[0][2].startswith(b"%PDF")
    completed = owner.get("/api/funnel-runs/" + run["id"]).json
    assert config["data"]["buyer_tag"] in completed["data"]["tags"]
    right_id = completed["data"]["guide_right_id"]
    assert owner.get(f"/api/guides/{right_id}/pdf").data == files[0][2]
    assert login(app, 3).get(f"/api/guides/{right_id}/pdf").status_code == 404
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='right'").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE object_id=? AND event LIKE 'funnel.followup.%'",
                (run["id"],),
            ).fetchone()[0]
            == 3
        )
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE object_id=? AND event LIKE 'funnel.cart.%' AND status='pending'",
                (run["id"],),
            ).fetchone()[0]
            == 0
        )
    for _ in range(3):
        app.clock[0] += 86400
        drain(app)
    assert "TEST_PROMO" in sent[-1][1]["text"]
    assert "сегодня в 23:59" not in sent[-1][1]["text"]


def test_refusal_stop_and_expiry_cancel_sales_messages(app):
    _, owner, run, config, _, sent, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    app.clock[0] += 1800
    drain(app)
    assert "не завершили оплату" in sent[-1][1]["text"]
    renewed = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert renewed["id"] != order["id"]
    assert renewed["data"]["expires"] <= run["data"]["offer_expires"]
    app.config["ENFORCE_CONSENT"] = True
    response = owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={})
    assert response.status_code == 200, response.json
    assert config["data"]["refused_tag"] in response.json["data"]["tags"]
    assert owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).status_code == 422
    count = len(sent)
    app.clock[0] += 7200
    drain(app)
    assert len(sent) == count


def test_booking_suppresses_followups_and_keeps_existing_scheduling_flow(app):
    admin, owner, run, _, _, sent, _ = setup_sale(app)
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Вторник, после 18:00 МСК"}
    ).json
    assert booking["status"] == "requested"
    assert (
        owner.post(f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Повтор"}).json["id"]
        == booking["id"]
    )
    proposed = admin.post(
        f"/api/admin/bookings/{booking['id']}/propose",
        json={
            "revision": booking["revision"],
            "reason": "Agree a time",
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
        },
    ).json
    drain(app)
    assert any("Подтвердите время" in data.get("text", "") for _, data in sent)
    assert (
        owner.post(
            f"/api/bookings/{booking['id']}/confirm",
            json={"proposal_id": proposed["data"]["current_proposal"]},
        ).json["status"]
        == "confirmed"
    )
    stopped = owner.post("/api/funnel/stop", json={})
    assert stopped.status_code == 200


def test_profile_changes_are_audited_and_partial_delivery_can_resume(app):
    admin, owner = login(app), login(app, 2)
    calls = []

    def transport(method, url, data, headers):
        method = url.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "setMyDescription" and calls.count(method) == 1:
            raise RuntimeError("Synthetic interruption")
        return {"ok": True, "result": True}

    app.config["HTTP_TRANSPORT"] = transport
    data = admin.get("/api/admin/funnel-profile").json
    assert (
        owner.post("/api/admin/funnel-profile", json={**data, "reason": "Fixture"}).status_code
        == 403
    )
    assert admin.post("/api/admin/funnel-profile", json=data).status_code == 422
    response = admin.post(
        "/api/admin/funnel-profile", json={**data, "reason": "Set owner-supplied profile"}
    )
    assert response.status_code == 202
    obj = response.json
    assert not calls
    drain(app)
    assert admin.get("/api/admin/objects/" + obj["id"]).json["object"]["status"] == "partial"
    app.clock[0] += 60
    drain(app)
    assert calls.count("setMyName") == 1 and calls.count("setMyDescription") == 2
    assert admin.get("/api/admin/objects/" + obj["id"]).json["object"]["status"] == "applied"
    assert admin.get(f"/api/admin/objects/{obj['id']}/versions").json["items"]


@pytest.mark.parametrize("stop_kind", ["client", "admin", "booking", "revoke"])
def test_paid_guide_retry_and_followup_cancellation(app, stop_kind):
    admin, owner, run, _, _, sent, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    transport = app.config["HTTP_TRANSPORT"]
    failed = []

    def failing_success(method, url, data, headers):
        if "Оплата прошла успешно" in data.get("text", "") and not failed:
            failed.append(True)
            raise RuntimeError("Synthetic sendMessage failure after sendDocument")
        return transport(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = failing_success
    drain(app)
    assert len(files) == 1
    app.clock[0] += 60
    drain(app)
    assert len(files) == 1
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    assert current["data"]["guide_delivered_at"]
    if stop_kind == "client":
        owner.post("/api/funnel/stop", json={})
    elif stop_kind == "admin":
        assert (
            owner.post(
                f"/api/admin/funnel-runs/{run['id']}/stop",
                json={"revision": current["revision"], "reason": "Fixture"},
            ).status_code
            == 403
        )
        assert (
            admin.post(
                f"/api/admin/funnel-runs/{run['id']}/stop",
                json={"revision": current["revision"], "reason": "Stop marketing"},
            ).status_code
            == 200
        )
    elif stop_kind == "booking":
        booking = owner.post(
            f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Вторник, 18:00 МСК"}
        ).json
        assert booking["data"]["promotion"]["eligible"] is True
        drain(app)
    else:
        right = admin.get("/api/admin/objects/" + current["data"]["guide_right_id"]).json["object"]
        revoked = admin.post(
            f"/api/admin/rights/{right['id']}/revoke",
            json={"revision": right["revision"], "reason": "Revoke test right"},
        )
        assert revoked.status_code == 200, revoked.json
        assert owner.get(f"/api/guides/{right['id']}/pdf").status_code == 403
    count = len(sent)
    app.clock[0] += 3 * 86400
    drain(app)
    assert len(sent) == count


@pytest.mark.parametrize("late", [False, True])
def test_unverified_or_late_payment_cannot_deliver_guide(app, late):
    admin, owner, run, _, _, _, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    if late:
        app.clock[0] += 901
    assert pay(app, order, amount=123 if late else 1).status_code == 200
    drain(app)
    assert not files
    assert not admin.get("/api/admin/objects?kind=right").json["items"]
    assert admin.get("/api/admin/objects?kind=reconciliation").json["items"]


def test_green_free_pdf_and_red_consultation_have_no_paid_gate(app):
    admin, owner = login(app), login(app, 2)
    base = scenario(admin)
    material = admin.post(
        f"/api/admin/funnels/{base['id']}/prepare-guide",
        json={"revision": base["revision"], "reason": "Synthetic free PDF"},
    ).json
    material = approve_existing(admin, material)
    scenario(admin, free_material_id=material["id"])
    sent, files = capture(app)
    green = finish(owner, start(owner), ["a"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    assert "Игры по возрасту" in str(sent[-1])
    assert owner.get(f"/api/funnel-runs/{green['id']}/free-pdf").data.startswith(b"%PDF")
    red = finish(owner, start(owner, key="red"), ["c"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    assert "О диагностике" in str(sent[-1])
    assert owner.get(f"/api/funnel-runs/{red['id']}/free-pdf").status_code == 403
    assert (
        "промокод"
        not in owner.get(f"/api/funnel-runs/{red['id']}/consultation").json["text"].lower()
    )
    assert (
        owner.post(
            f"/api/funnel-runs/{red['id']}/book", json={"preferences": "Завтра утром МСК"}
        ).json["status"]
        == "requested"
    )


def test_refusal_can_deliver_the_configured_free_video(app, tmp_path):
    import io
    from tests.test_crm import make_video

    admin, owner = login(app), login(app, 2)
    raw = make_video(tmp_path)
    material = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Synthetic free video",
            "data": {"owner": "Fixture specialist", "status": "received"},
            "reason": "Fixture free video",
        },
    ).json
    asset = admin.post(
        f"/api/admin/content/{material['id']}/asset",
        data={"reason": "Fixture MP4", "file": (io.BytesIO(raw), "fixture.mp4", "video/mp4")},
    ).json
    material = admin.patch(
        f"/api/admin/content/{material['id']}",
        json={
            "revision": material["revision"],
            "data": {**material["data"], "asset_id": asset["id"]},
            "reason": "Attach reviewed fixture video",
        },
    ).json
    material = approve_existing(admin, material)
    scenario(admin, refusal_material_id=material["id"])
    _, files = capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    assert owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={}).status_code == 200
    app.clock[0] += 4 * 86400
    drain(app)
    assert len(files) == 1
    assert files[0][2] == raw and files[0][4] == "sendVideo"
