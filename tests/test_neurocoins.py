"""Neurocoin wallet: no client funds are credited before a verified Lava receipt."""

import concurrent.futures
import sqlite3

import pytest

from marina import Problem
from marina.commerce import confirm
from marina.db import connect, insert, transaction, unpack
from marina.neurocoins import entry, validate_policy
from marina.operations import work_once
from marina.providers import check_lava
from tests.test_identity import app as identity_app
from tests.test_identity import login


@pytest.fixture
def app(tmp_path):
    return identity_app.__wrapped__(tmp_path)


@pytest.fixture
def client_and_admin(app):
    app.config.update(
        NEUROCOINS_LAVA_ENABLED=True,
        NEUROCOINS_BOT_SPEND_ENABLED=True,
        NEUROCOINS_WEB_ORIGIN="https://wallet.example.test",
        LAVA_API_KEY="fixture-key",
        LAVA_WEBHOOK_SECRET="fixture-secret",
        PUBLIC_URL="https://fixture.example",
        ENABLED_PAYMENT_PROVIDERS=["lava"],
    )
    return login(app, 2), login(app, 1)


WEB_REQUEST = {
    "base_url": "https://wallet.example.test",
    "headers": {
        "Origin": "https://wallet.example.test",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
    },
}


def publish_policy(app, *, enabled=True, coins=150, price=120):
    policy = {
        "type": "neurocoins",
        "currency": "RUB",
        "title": "Нейро-коины",
        "enabled": enabled,
        "terms": "Нейро-коины расходуются только на услуги; вывод и перевод недоступны.",
        "quote_ttl_seconds": 3600,
        "packages": [
            {"id": "p150", "title": "150 НК", "coins": coins, "lava_offer_id": "offer-fixture"}
        ],
        "services": [{"id": "online_lesson", "title": "Занятие", "coins": price}],
    }
    with transaction(app.config["DATABASE"]) as db:
        return insert(db, "policy", policy, app.clock[0], status="published")


def owner_id(client):
    return client.get("/api/me").json["account"]["id"]


def booking_confirmed(app, client, admin):
    child = client.post(
        "/api/subjects",
        json={"kind": "child", "name": "Fixture", "birth_date": "2020-01-01"},
    )
    assert child.status_code == 201, child.json
    booking = client.post(
        "/api/bookings",
        json={
            "subject_id": child.json["id"],
            "topic": "Фикстура",
            "preferences": "Любой день",
            "booking_kind": "online_lesson",
            "key": "booking-fixture",
        },
    )
    assert booking.status_code == 200, booking.json
    proposal = admin.post(
        "/api/admin/bookings/" + booking.json["id"] + "/propose",
        json={
            "revision": booking.json["revision"],
            "starts": app.clock[0] + 3600,
            "duration_minutes": 45,
            "timezone": "Europe/Moscow",
            "reason": "Проверка записи",
        },
    )
    assert proposal.status_code == 200, proposal.json
    response = client.post(
        "/api/bookings/" + booking.json["id"] + "/confirm",
        json={"proposal_id": proposal.json["data"]["current_proposal"]},
    )
    assert response.status_code == 200, response.json
    return response.json


def topup(client, *, booking_id=None, key="fixture-topup", surface="external_web"):
    return client.post(
        "/api/wallet/topups",
        json={
            "package_id": "p150",
            "key": key,
            "booking_id": booking_id,
            "surface": surface,
            "policy_id": wallet(client)["policy_id"],
            "terms_accepted": True,
        },
        **WEB_REQUEST,
    )


def confirmed_receipt(app, order_id, *, charge="lava-charge-fixture", amount=15000):
    with transaction(app.config["DATABASE"]) as db:
        order = unpack(db.execute("SELECT * FROM objects WHERE id=?", (order_id,)).fetchone())
        return confirm(
            db,
            order,
            "lava",
            charge,
            amount,
            "RUB",
            app.clock[0],
            app.clock[0],
        )


def wallet(client):
    response = client.get("/api/wallet")
    assert response.status_code == 200, response.json
    return response.json


def test_external_wallet_page_is_visible_but_checkout_disabled_by_default(app):
    response = app.test_client().get("/wallet")
    assert response.status_code == 200
    assert b"wallet.js" in response.data
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    static_response = app.test_client().get("/static/wallet.html")
    assert static_response.status_code == 200
    assert "frame-ancestors 'none'" in static_response.headers["Content-Security-Policy"]
    assert (
        app.test_client().post("/api/wallet/topups", json={"surface": "external_web"}).status_code
        == 401
    )


def test_external_topup_cannot_be_activated_by_client_surface_alone(app, client_and_admin):
    owner, admin = client_and_admin
    publish_policy(app)
    original = WEB_REQUEST
    payload = {
        "package_id": "p150",
        "key": "origin-attack",
        "surface": "external_web",
        "policy_id": wallet(owner)["policy_id"],
        "terms_accepted": True,
    }
    denied = [
        {"base_url": "http://wallet.example.test", "headers": original["headers"]},
        {"base_url": "https://other.example.test", "headers": original["headers"]},
        {"base_url": original["base_url"], "headers": {}},
        {
            "base_url": original["base_url"],
            "headers": {
                **original["headers"],
                "Origin": "https://evil.example.test",
            },
        },
        {
            "base_url": original["base_url"],
            "headers": {
                **original["headers"],
                "Sec-Fetch-Site": "cross-site",
            },
        },
        {
            "base_url": original["base_url"],
            "headers": {
                **original["headers"],
                "Sec-Fetch-Mode": "navigate",
            },
        },
    ]
    for arguments in denied:
        result = owner.post("/api/wallet/topups", json=payload, **arguments)
        assert result.status_code == 403, result.json
    assert wallet(owner)["balance"] == 0
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='order'").fetchone()[0] == 0


def test_external_topup_requires_published_terms_snapshot(app, client_and_admin):
    owner, admin = client_and_admin
    published = publish_policy(app)
    missing = owner.post(
        "/api/wallet/topups",
        json={"package_id": "p150", "key": "missing-terms", "surface": "external_web"},
        **WEB_REQUEST,
    )
    assert missing.status_code == 422, missing.json
    wrong = owner.post(
        "/api/wallet/topups",
        json={
            "package_id": "p150",
            "key": "old-terms",
            "surface": "external_web",
            "policy_id": "wrong-policy",
            "terms_accepted": True,
        },
        **WEB_REQUEST,
    )
    assert wrong.status_code == 422, wrong.json
    approved = topup(owner)
    assert approved.status_code == 201, approved.json
    assert approved.json["data"]["policy_id"] == published["id"]
    assert approved.json["data"]["terms_accepted_at"] == app.clock[0]


def test_wallet_is_private_and_empty_by_default(app):
    client = login(app, 2)
    other = login(app, 3)
    assert wallet(client)["balance"] == 0
    assert wallet(client)["enabled"] is False
    assert wallet(other)["history"] == []
    blocked = topup(client)
    assert blocked.status_code == 403
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM neurocoin_ledger").fetchone()[0] == 0


def test_wallet_ledger_is_in_owners_data_export_only(app, client_and_admin):
    owner, admin = client_and_admin
    another = login(app, 3)
    owner_account = owner_id(owner)
    response = admin.post(
        "/api/admin/wallet/" + owner_account + "/adjust",
        json={"delta": 25, "key": "export-proof", "reason": "Тест выгрузки"},
    )
    assert response.status_code == 200, response.json
    exported = owner.get("/api/data-export")
    assert exported.status_code == 200, exported.json
    assert len(exported.json["neurocoin_ledger"]) == 1
    assert exported.json["neurocoin_ledger"][0]["delta"] == 25
    isolated = another.get("/api/data-export")
    assert isolated.status_code == 200
    assert isolated.json["neurocoin_ledger"] == []


def test_bot_spending_is_closed_by_default_even_after_admin_credit(app):
    client, admin = login(app, 2), login(app)
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    person = owner_id(client)
    result = admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={"delta": 200, "key": "blocked-spend", "reason": "Тест ограничений"},
    )
    assert result.status_code == 200
    assert wallet(client)["balance"] == 200
    assert wallet(client)["spend_enabled"] is False
    blocked = client.post(
        "/api/wallet/bookings/" + booking["id"] + "/pay", json={"key": "closed-gate"}
    )
    assert blocked.status_code == 403
    assert wallet(client)["balance"] == 200


def test_policy_rejects_stars_and_invalid_amounts(app):
    valid = {
        "type": "neurocoins",
        "currency": "RUB",
        "enabled": True,
        "terms": "Только услуги",
        "quote_ttl_seconds": 3600,
        "packages": [{"id": "p150", "title": "150 НК", "coins": 150, "lava_offer_id": "fixture"}],
        "services": [{"id": "online_lesson", "title": "Занятие", "coins": 120}],
    }
    validate_policy(valid)
    with pytest.raises(Problem):
        validate_policy({**valid, "currency": "XTR"})
    with pytest.raises(Problem):
        validate_policy(
            {**valid, "packages": [{"id": "p150", "title": "Stars", "coins": 150, "stars": 150}]}
        )
    with pytest.raises(Problem):
        validate_policy(
            {
                **valid,
                "packages": [
                    {
                        "id": "p150",
                        "title": "Stars plus Lava",
                        "coins": 150,
                        "lava_offer_id": "fixture",
                        "stars": 150,
                    }
                ],
            }
        )
    with pytest.raises(Problem):
        validate_policy(
            {
                **valid,
                "packages": [
                    {"id": "p150", "title": "Пакет", "coins": True, "lava_offer_id": "fixture"}
                ],
            }
        )
    with pytest.raises(Problem):
        validate_policy(
            {**valid, "services": [{"id": "online_lesson", "title": "Пакет", "coins": -1}]}
        )


def test_admin_publishes_pack_and_tariff_through_existing_workflow(app, client_and_admin):
    client, admin = client_and_admin
    data = {
        "type": "neurocoins",
        "enabled": True,
        "currency": "RUB",
        "terms": "Утвержденные условия",
        "quote_ttl_seconds": 3600,
        "packages": [
            {"id": "p150", "title": "150 НК", "coins": 150, "lava_offer_id": "fixture-offer"}
        ],
        "services": [{"id": "online_lesson", "title": "Занятие", "coins": 120}],
    }
    created = admin.post(
        "/api/admin/content",
        json={
            "kind": "policy",
            "title": "Нейро-коины",
            "data": data,
            "reason": "Тест создания тарифов",
        },
    )
    assert created.status_code == 201, created.json
    approved = admin.post(
        "/api/admin/content/" + created.json["id"] + "/approve",
        json={"revision": created.json["revision"], "reason": "Тест одобрения"},
    )
    assert approved.status_code == 200, approved.json
    published = admin.post(
        "/api/admin/content/" + created.json["id"] + "/publish",
        json={"revision": approved.json["revision"], "reason": "Тест публикации"},
    )
    assert published.status_code == 200, published.json
    current = wallet(client)
    assert current["enabled"] is True
    assert current["packages"][0]["coins"] == 150
    assert current["services"][0]["coins"] == 120


def test_bot_spend_gate_stays_closed_when_policy_is_published(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    app.config["NEUROCOINS_BOT_SPEND_ENABLED"] = False
    assert wallet(client)["spend_enabled"] is False
    response = client.post(
        "/api/wallet/bookings/" + booking["id"] + "/pay",
        json={"key": "should-not-spend"},
    )
    assert response.status_code == 403
    assert wallet(client)["balance"] == 0


def test_upgrade_existing_database_preserves_accounts_and_creates_wallet_tables(app):
    client = login(app, 2)
    person = owner_id(client)
    with connect(app.config["DATABASE"]) as db:
        db.execute("DROP TABLE neurocoin_ledger")
        assert db.execute("SELECT count(*) FROM accounts WHERE id=?", (person,)).fetchone()[0] == 1
    from marina import create_app

    upgraded = create_app({**app.config})
    with connect(upgraded.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM accounts WHERE id=?", (person,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM neurocoin_ledger").fetchone()[0] == 0
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        triggers = db.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'neurocoin_ledger_no_%'"
        ).fetchone()[0]
        assert triggers == 2


def test_ledger_rows_are_immutable_at_database_level(app, client_and_admin):
    client, admin = client_and_admin
    person = owner_id(client)
    adjustment = admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={"delta": 40, "key": "immutable-row", "reason": "Проверка неизменности"},
    )
    assert adjustment.status_code == 200, adjustment.json
    entry_id = adjustment.json["entry"]["id"]
    with connect(app.config["DATABASE"]) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE neurocoin_ledger SET delta=100 WHERE id=?", (entry_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM neurocoin_ledger WHERE id=?", (entry_id,))
    assert wallet(client)["balance"] == 40


def test_admin_adjustment_requires_real_admin_and_is_idempotent(app, client_and_admin):
    client, admin = client_and_admin
    person = owner_id(client)
    base = {"delta": 200, "key": "adjust-123", "reason": "Тестовое начисление"}
    assert client.post("/api/admin/wallet/" + person + "/adjust", json=base).status_code == 403
    first = admin.post("/api/admin/wallet/" + person + "/adjust", json=base)
    assert first.status_code == 200, first.json
    second = admin.post("/api/admin/wallet/" + person + "/adjust", json=base)
    assert second.status_code == 200
    assert wallet(client)["balance"] == 200
    conflict = admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={**base, "delta": 201},
    )
    assert conflict.status_code == 409
    app.config["ADMIN_TELEGRAM_IDS"] = set()
    assert (
        admin.post(
            "/api/admin/wallet/" + person + "/adjust",
            json={**base, "key": "adjust-456"},
        ).status_code
        == 403
    )
    assert wallet(client)["balance"] == 200


def test_confirmed_booking_debit_then_cancel_refunds_once(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    person = owner_id(client)
    credit = admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={"delta": 150, "key": "adjust-200", "reason": "Тестовые монеты"},
    )
    assert credit.status_code == 200
    pay = client.post(
        "/api/wallet/bookings/" + booking["id"] + "/pay",
        json={"key": "pay-1234"},
    )
    assert pay.status_code == 200, pay.json
    assert wallet(client)["balance"] == 30
    duplicate = client.post(
        "/api/wallet/bookings/" + booking["id"] + "/pay",
        json={"key": "pay-1234"},
    )
    assert duplicate.status_code == 200, duplicate.json
    assert wallet(client)["balance"] == 30
    cancel = client.post("/api/bookings/" + booking["id"] + "/cancel", json={})
    assert cancel.status_code == 200, cancel.json
    # Refunds require a separate audited decision by the specialist.
    assert wallet(client)["balance"] == 30
    with connect(app.config["DATABASE"]) as db:
        rec = db.execute(
            "SELECT status FROM objects WHERE kind='reconciliation' "
            "AND json_extract(data,'$.booking_id')=?",
            (booking["id"],),
        ).fetchone()
        assert rec is not None and rec["status"] == "open"
    refund = admin.post(
        "/api/admin/wallet/bookings/" + booking["id"] + "/refund",
        json={"reason": "Согласован возврат за отменённое занятие"},
    )
    assert refund.status_code == 200, refund.json
    assert wallet(client)["balance"] == 150
    again = admin.post(
        "/api/admin/wallet/bookings/" + booking["id"] + "/refund",
        json={"reason": "Повторное подтверждение возврата"},
    )
    assert again.status_code == 200
    assert wallet(client)["balance"] == 150
    with connect(app.config["DATABASE"]) as db:
        source_counts = db.execute(
            "SELECT source,count(*) FROM neurocoin_ledger GROUP BY source"
        ).fetchall()
    assert {x[0]: x[1] for x in source_counts} == {
        "admin_adjustment": 1,
        "booking": 1,
        "booking_refund": 1,
    }


def test_insufficient_balance_cannot_pay_or_create_debit(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    pay = client.post(
        "/api/wallet/bookings/" + booking["id"] + "/pay",
        json={"key": "pay-insufficient"},
    )
    assert pay.status_code == 409
    assert wallet(client)["balance"] == 0
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM neurocoin_ledger").fetchone()[0] == 0


def test_lava_topup_is_external_only_and_never_credits_before_receipt(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    assert topup(client, surface="telegram").status_code == 403
    order = topup(client)
    assert order.status_code == 201, order.json
    assert order.json["data"]["provider"] == "lava"
    assert order.json["data"]["total"] == 15000
    assert order.json["data"]["currency"] == "RUB"
    assert wallet(client)["balance"] == 0
    repeated = topup(client)
    assert repeated.status_code == 201, repeated.json
    assert repeated.json["id"] == order.json["id"]


def test_same_key_replays_pending_auto_topup_without_second_order(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    first = topup(client, booking_id=booking["id"], key="same-key")
    assert first.status_code == 201, first.json
    again = topup(client, booking_id=booking["id"], key="same-key")
    assert again.status_code == 201, again.json
    assert again.json["id"] == first.json["id"]
    other_key = topup(client, booking_id=booking["id"], key="other-key")
    assert other_key.status_code == 409
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='order'").fetchone()[0] == 1


def test_existing_balance_combines_with_topup_and_auto_purchase(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app, coins=150, price=210)
    booking = booking_confirmed(app, client, admin)
    person = owner_id(client)
    seed = admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={"delta": 100, "key": "init-balance", "reason": "Тестовый остаток"},
    )
    assert seed.status_code == 200
    order = topup(client, booking_id=booking["id"], key="topup-combine")
    assert order.status_code == 201, order.json
    confirmed_receipt(app, order.json["id"])
    assert wallet(client)["balance"] == 40
    booked = client.get("/api/bookings/" + booking["id"]).json
    assert booked["data"]["neurocoins_paid"] == 210


def test_external_invoice_is_visible_only_to_owner_not_sent_into_telegram(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    order = topup(client)
    assert order.status_code == 201, order.json
    order_id = order.json["id"]
    other = login(app, 3)
    assert other.get("/api/wallet/topups/" + order_id).status_code == 404
    assert client.get("/api/wallet/topups/" + order_id).json["payment_url"] is None

    attempt = client.post(
        "/api/orders/" + order_id + "/checkout",
        json={"email": "buyer@example.test"},
        **WEB_REQUEST,
    )
    assert attempt.status_code == 202, attempt.json
    requests = []

    def fake_provider(method, url, data, headers):
        requests.append((method, url, data))
        if url.endswith("/api/v3/invoice"):
            assert data["currency"] == "RUB"
            assert data["amount"] == 150
            assert data["offerId"] == "offer-fixture"
            assert data["successful_return_url"] == (
                "https://wallet.example.test/wallet?payment=success"
            )
            assert data["failure_return_url"] == (
                "https://wallet.example.test/wallet?payment=failed"
            )
            assert data["cancel_return_url"] == (
                "https://wallet.example.test/wallet?payment=cancelled"
            )
            return {"id": "lava-sandbox", "paymentUrl": "https://checkout.example.test/pay"}
        return {"ok": True, "result": {"message_id": len(requests)}}

    app.config["HTTP_TRANSPORT"] = fake_provider
    for _ in range(30):
        with connect(app.config["DATABASE"]) as db:
            attempt_status = db.execute(
                "SELECT status FROM objects WHERE id=?", (attempt.json["id"],)
            ).fetchone()[0]
        if attempt_status == "ready":
            break
        assert work_once(app) == 1
    assert attempt_status == "ready"
    leaked = client.get("/api/wallet/topups/" + order_id)
    assert leaked.status_code == 200
    assert leaked.json["payment_url"] is None
    view = client.get("/api/wallet/topups/" + order_id, base_url="https://wallet.example.test")
    assert view.status_code == 200
    assert view.json["payment_url"] == "https://checkout.example.test/pay"
    app.config["NEUROCOINS_LAVA_ENABLED"] = False
    hidden = client.get("/api/wallet/topups/" + order_id)
    assert hidden.status_code == 200
    assert hidden.json["payment_url"] is None
    app.config["NEUROCOINS_LAVA_ENABLED"] = True
    assert wallet(client)["balance"] == 0
    with connect(app.config["DATABASE"]) as db:
        invoice_notifications = db.execute(
            "SELECT count(*) FROM outbox WHERE event='invoice.ready'"
        ).fetchone()[0]
    assert invoice_notifications == 0
    assert len([x for x in requests if x[1].endswith("/api/v3/invoice")]) == 1
    assert not any(
        x[1].endswith("/sendMessage") and "checkout.example" in str(x[2]) for x in requests
    )
    replay = client.post(
        "/api/orders/" + order_id + "/checkout",
        json={"email": "buyer@example.test"},
        **WEB_REQUEST,
    )
    assert replay.status_code == 200
    assert replay.json["id"] == attempt.json["id"]


def test_disabling_lava_suppresses_queued_invoice_without_provider_call(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    order = topup(client)
    assert order.status_code == 201
    attempt = client.post(
        "/api/orders/" + order.json["id"] + "/checkout",
        json={"email": "buyer@example.test"},
        **WEB_REQUEST,
    )
    assert attempt.status_code == 202, attempt.json
    app.config["NEUROCOINS_LAVA_ENABLED"] = False

    def no_provider(*args):
        pytest.fail("Suspended Lava invoice reached provider")

    app.config["HTTP_TRANSPORT"] = no_provider
    for _ in range(12):
        with connect(app.config["DATABASE"]) as db:
            status = db.execute(
                "SELECT status FROM objects WHERE id=?", (attempt.json["id"],)
            ).fetchone()[0]
        if status != "queued":
            break
        assert work_once(app) == 1
    assert status == "cancelled"
    assert wallet(client)["balance"] == 0
    view = client.get("/api/wallet/topups/" + order.json["id"])
    assert view.status_code == 200
    assert view.json["payment_url"] is None


def test_verified_provider_lookup_creates_ledger_credit(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    order = topup(client)
    attempt = client.post(
        "/api/orders/" + order.json["id"] + "/checkout",
        json={"email": "buyer@example.test"},
        **WEB_REQUEST,
    )
    assert attempt.status_code == 202
    calls = []

    def fake_remote(method, url, data, headers):
        calls.append((method, url))
        if method == "GET" and "/api/v1/invoices/" in url:
            return {
                "id": "paid-invoice-1",
                "status": "completed",
                "datetime": "2026-11-14T07:08:20Z",
                "receipt": {"amount": "150", "currency": "RUB"},
            }
        return {"ok": True, "result": None}

    app.config["HTTP_TRANSPORT"] = fake_remote
    person = owner_id(client)
    with transaction(app.config["DATABASE"]) as db:
        from marina import object_get
        from marina.db import update

        payment = object_get(db, attempt.json["id"], "payment_attempt", person)
        payment = update(
            db,
            payment,
            app.clock[0],
            {**payment["data"], "provider_id": "paid-invoice-1"},
            "ready",
        )
        result = check_lava(app, db, payment, app.clock[0])
        assert result["status"] == "paid"
    assert wallet(client)["balance"] == 150
    assert calls == [("GET", "https://gate.lava.top/api/v1/invoices/paid-invoice-1")]


def test_verified_lava_receipt_auto_purchases_only_once(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    order = topup(client, booking_id=booking["id"])
    assert order.status_code == 201, order.json
    assert wallet(client)["balance"] == 0
    result = confirmed_receipt(app, order.json["id"])
    assert result["status"] == "paid"
    assert wallet(client)["balance"] == 30
    details = client.get("/api/bookings/" + booking["id"]).json
    assert details["data"]["payment_source"] == "neurocoins"
    assert details["data"]["neurocoins_paid"] == 120
    assert details["data"]["paid_at"] == app.clock[0]
    confirmed_receipt(app, order.json["id"])
    assert wallet(client)["balance"] == 30
    with connect(app.config["DATABASE"]) as db:
        ledger = list(db.execute("SELECT source,delta FROM neurocoin_ledger ORDER BY created,id"))
    assert sorted((r["source"], r["delta"]) for r in ledger) == [
        ("booking", -120),
        ("lava_credit", 150),
    ]


def test_cancelled_topup_cannot_auto_purchase_even_if_provider_paid(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    order = topup(client, booking_id=booking["id"])
    assert order.status_code == 201
    from marina import object_get
    from marina.db import update

    person = owner_id(client)
    with transaction(app.config["DATABASE"]) as db:
        previous = object_get(db, order.json["id"], "order", person)
        update(db, previous, app.clock[0], status="cancelled")
    result = confirmed_receipt(app, order.json["id"])
    assert result["status"] == "paid"
    assert wallet(client)["balance"] == 150
    active = client.get("/api/bookings/" + booking["id"]).json
    assert active["data"].get("paid_at") is None


def test_cancelled_booking_preserves_lava_funds_without_auto_purchase(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    booking = booking_confirmed(app, client, admin)
    order = topup(client, booking_id=booking["id"])
    assert order.status_code == 201
    cancel = client.post("/api/bookings/" + booking["id"] + "/cancel", json={})
    assert cancel.status_code == 200
    result = confirmed_receipt(app, order.json["id"])
    assert result["status"] == "paid"
    assert wallet(client)["balance"] == 150
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT count(*) FROM neurocoin_ledger WHERE source='booking'").fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM objects WHERE kind='reconciliation' "
                "AND json_extract(data,'$.issue')='neurocoins_booking_cancelled'"
            ).fetchone()[0]
            == 0
        )


def test_lava_mismatch_causes_reconciliation_not_credit(app, client_and_admin):
    client, admin = client_and_admin
    publish_policy(app)
    order = topup(client)
    assert order.status_code == 201
    confirmed_receipt(app, order.json["id"], amount=14900)
    assert wallet(client)["balance"] == 0
    with connect(app.config["DATABASE"]) as db:
        rec = db.execute(
            "SELECT status FROM receipts WHERE order_id=?", (order.json["id"],)
        ).fetchone()
        assert rec["status"] == "amount_currency_or_owner_mismatch"
        assert db.execute("SELECT count(*) FROM neurocoin_ledger").fetchone()[0] == 0


def test_two_concurrent_spends_cannot_overdraw(app, client_and_admin):
    client, admin = client_and_admin
    person = owner_id(client)
    admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={"delta": 100, "key": "seed-100", "reason": "Начальный баланс"},
    )

    def spend(key):
        try:
            with transaction(app.config["DATABASE"]) as db:
                entry(db, person, -75, "concurrent", key, app.clock[0], person, "Test")
            return True
        except Problem:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(spend, ["one", "two"]))
    assert sorted(results) == [False, True]
    assert wallet(client)["balance"] == 25
