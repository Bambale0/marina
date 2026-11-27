"""T-Bank API and webhook regression tests: zero real financial requests."""

import hashlib

from marina.commerce import provider_enabled, rub_provider
from marina.db import connect, insert, transaction
from marina.tbank import init_invoice, sign, verify_notification
import pytest
from tests.test_identity import app as fixture_app, login


@pytest.fixture
def app(tmp_path):
    return fixture_app.__wrapped__(tmp_path)


def settings(app):
    app.config.update(
        TBANK_TERMINAL_KEY="TEST_TERMINAL",
        TBANK_PASSWORD="fixture-password",
        TBANK_API_URL="https://securepay.tinkoff.ru",
        PUBLIC_URL="https://marina.example.test",
        ENABLED_PAYMENT_PROVIDERS=["tbank", "lava"],
        PRIMARY_RUB_PROVIDER="tbank",
    )


def signature(data, password):
    def value(x):
        return "true" if x is True else "false" if x is False else str(x)

    fields = {
        k: v
        for k, v in data.items()
        if k != "Token" and v is not None and not isinstance(v, (dict, list))
    }
    fields["Password"] = password
    return hashlib.sha256("".join(value(fields[k]) for k in sorted(fields)).encode()).hexdigest()


def order_and_attempt(app):
    owner = login(app, 2).get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        order = insert(
            db,
            "order",
            {
                "intent_id": "test-tbank-intent",
                "product_id": "fixture-product",
                "product_snapshot": {"service": "guide", "title": "Тест"},
                "lines": [],
                "total": 10000,
                "currency": "RUB",
                "provider": "tbank",
                "expires": app.clock[0] + 3600,
            },
            app.clock[0],
            owner,
            status="pending",
        )
        attempt = insert(
            db,
            "payment_attempt",
            {
                "order_id": order["id"],
                "provider": "tbank",
                "total": 10000,
                "currency": "RUB",
                "title": "Тест",
                "terms": "test",
                "provider_id": "100000123",
                "return_path": "/",
            },
            app.clock[0],
            owner,
            status="ready",
        )
    return order, attempt


def notification(attempt, *, status="CONFIRMED", payment_id="100000123", amount=10000):
    data = {
        "TerminalKey": "TEST_TERMINAL",
        "OrderId": attempt["id"],
        "PaymentId": payment_id,
        "Amount": amount,
        "Status": status,
        "Success": True,
        "ErrorCode": "0",
    }
    data["Token"] = signature(data, "fixture-password")
    return data


def test_sign_excludes_nested_fields_and_verifies_callback(app):
    settings(app)
    data = {"TerminalKey": "TEST_TERMINAL", "Amount": 10000, "Success": True, "Data": {"test": 1}}
    assert sign(data, "fixture-password") == signature(data, "fixture-password")
    data["Token"] = signature(data, "fixture-password")
    assert verify_notification(app, data)
    data["Amount"] = 1
    assert not verify_notification(app, data)


def test_tbank_selected_as_primary_when_enabled(app):
    assert rub_provider(app) == "lava"
    settings(app)
    assert provider_enabled(app, "tbank")
    assert rub_provider(app) == "tbank"
    app.config["TBANK_PASSWORD"] = ""
    assert not provider_enabled(app, "tbank")


def test_tbank_init_uses_cents_durable_id_and_signature(app):
    settings(app)
    order, attempt = order_and_attempt(app)
    captured = []

    def fake(method, url, data, headers):
        captured.append((method, url, data))
        return {
            "Success": True,
            "PaymentId": "100000123",
            "PaymentURL": "https://securepay.tinkoff.ru/fixture",
        }

    app.config["HTTP_TRANSPORT"] = fake
    result = init_invoice(app, attempt)
    assert result == {"url": "https://securepay.tinkoff.ru/fixture", "provider_id": "100000123"}
    method, url, payload = captured[0]
    assert method == "POST" and url == "https://securepay.tinkoff.ru/v2/Init"
    assert payload["Amount"] == order["data"]["total"]
    assert payload["OrderId"] == attempt["id"]
    assert payload["NotificationURL"] == "https://marina.example.test/api/webhooks/tbank"
    token = payload.pop("Token")
    assert token == signature(payload, "fixture-password")


def test_tbank_confirmed_webhook_rechecks_getstate_then_idempotent_receipt(app):
    settings(app)
    order, attempt = order_and_attempt(app)
    calls = []

    def fake(method, url, data, headers):
        calls.append((method, url))
        assert method == "POST" and url.endswith("/v2/GetState")
        assert data["PaymentId"] == "100000123"
        return {
            "Success": True,
            "PaymentId": "100000123",
            "OrderId": attempt["id"],
            "Amount": 10000,
            "TerminalKey": "TEST_TERMINAL",
            "Status": "CONFIRMED",
        }

    app.config["HTTP_TRANSPORT"] = fake
    client = app.test_client()
    request = notification(attempt)
    assert client.post("/api/webhooks/tbank", json={**request, "Token": "bad"}).status_code == 401
    assert (
        client.post(
            "/api/webhooks/tbank", json=notification(attempt, status="AUTHORIZED")
        ).status_code
        == 200
    )
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    first = client.post("/api/webhooks/tbank", json=request)
    assert first.status_code == 200 and first.data == b"OK"
    assert client.post("/api/webhooks/tbank", json=request).status_code == 200
    with connect(app.config["DATABASE"]) as db:
        receipts = db.execute("SELECT provider,charge,amount,status FROM receipts").fetchall()
        assert [tuple(row) for row in receipts] == [("tbank", "100000123", 10000, "confirmed")]
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert calls


def test_tbank_getstate_mismatch_blocks_credit(app):
    settings(app)
    order, attempt = order_and_attempt(app)
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: {
        "Success": True,
        "PaymentId": "100000123",
        "OrderId": "another-order",
        "Amount": 10000,
        "TerminalKey": "TEST_TERMINAL",
        "Status": "CONFIRMED",
    }
    response = app.test_client().post("/api/webhooks/tbank", json=notification(attempt))
    # Financial event is persisted for retry/reconciliation, but no receipt is minted.
    assert response.status_code in (200, 503)
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_tbank_wallet_topup_from_independent_web_mints_only_after_getstate(app):
    from marina.operations import work_once
    from tests.test_neurocoins import publish_policy

    settings(app)
    app.config.update(
        NEUROCOINS_TBANK_ENABLED=True,
        NEUROCOINS_LAVA_ENABLED=False,
        NEUROCOINS_WEB_ORIGIN="https://wallet.example.test",
    )
    publish_policy(app)
    owner = login(app, 2)
    web = {
        "base_url": "https://wallet.example.test",
        "headers": {
            "Origin": "https://wallet.example.test",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        },
    }
    p = owner.get("/api/wallet").json
    assert p["balance"] == 0
    assert p["web_topup_enabled"] is False  # default client origin is not independent web.
    topup = owner.post(
        "/api/wallet/topups",
        json={
            "surface": "external_web",
            "key": "tbank-wallet-fixture",
            "package_id": "p150",
            "policy_id": p["policy_id"],
            "terms_accepted": True,
        },
        **web,
    )
    assert topup.status_code == 201, topup.json
    order = topup.json
    assert order["data"]["provider"] == "tbank"

    state = {}

    def fake(method, url, data, headers):
        assert method == "POST"
        if url.endswith("/v2/Init"):
            assert data["Amount"] == 15000
            state["attempt_id"] = data["OrderId"]
            return {
                "Success": True,
                "PaymentId": "100000999",
                "PaymentURL": "https://securepay.tinkoff.ru/fixture",
            }
        assert url.endswith("/v2/GetState")
        return {
            "Success": True,
            "PaymentId": "100000999",
            "OrderId": state["attempt_id"],
            "Amount": 15000,
            "TerminalKey": "TEST_TERMINAL",
            "Status": "CONFIRMED",
        }

    app.config["HTTP_TRANSPORT"] = fake
    checkout = owner.post("/api/orders/" + order["id"] + "/checkout", json={}, **web)
    assert checkout.status_code == 202, checkout.json
    assert work_once(app, send=lambda *args: None) == 1
    assert owner.get("/api/wallet").json["balance"] == 0

    callback = {
        "TerminalKey": "TEST_TERMINAL",
        "PaymentId": "100000999",
        "OrderId": state["attempt_id"],
        "Status": "CONFIRMED",
        "Success": True,
        "ErrorCode": "0",
        "Amount": 15000,
    }
    callback["Token"] = signature(callback, "fixture-password")
    paid = app.test_client().post("/api/webhooks/tbank", json=callback)
    assert paid.status_code == 200 and paid.data == b"OK"
    assert owner.get("/api/wallet").json["balance"] == 150
    assert app.test_client().post("/api/webhooks/tbank", json=callback).status_code == 200
    assert owner.get("/api/wallet").json["balance"] == 150
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts WHERE provider='tbank'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM neurocoin_ledger").fetchone()[0] == 1


def test_tbank_refund_notification_creates_review_not_automatic_chargeback(app):
    settings(app)
    _, attempt = order_and_attempt(app)
    payload = notification(attempt, status="REFUNDED")
    response = app.test_client().post("/api/webhooks/tbank", json=payload)
    assert response.status_code == 200 and response.data == b"OK"
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM objects WHERE kind='reconciliation' AND status='open'"
            ).fetchone()[0]
            == 1
        )
    assert app.test_client().post("/api/webhooks/tbank", json=payload).status_code == 200
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='reconciliation'").fetchone()[0]
            == 1
        )

    # Second partial refund with a changed amount is a distinct financial event.
    later = {**payload, "Amount": 5000}
    later["Token"] = signature(later, "fixture-password")
    assert app.test_client().post("/api/webhooks/tbank", json=later).status_code == 200
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='reconciliation'").fetchone()[0]
            == 2
        )


def test_payment_return_links_to_configured_bot(app):
    app.config["BOT_USERNAME"] = "fixture_prod_bot"
    client = app.test_client()
    response = client.get("/?payment=success")
    assert response.status_code == 200
    assert b'href="/bot"' in response.data
    assert b"abrakadabra25_bot" not in response.data
    response = client.get("/bot")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://t.me/fixture_prod_bot"
    assert response.headers["Cache-Control"] == "no-store"
    app.config["BOT_USERNAME"] = "fixture_dev_bot"
    assert client.get("/bot").headers["Location"] == "https://t.me/fixture_dev_bot"
    app.config["BOT_USERNAME"] = ""
    assert client.get("/bot").status_code == 503
    app.config["BOT_USERNAME"] = "https://example.test"
    assert client.get("/bot").status_code == 503


def queue_invoice(app):
    from marina.db import emit, update

    settings(app)
    order, attempt = order_and_attempt(app)
    with transaction(app.config["DATABASE"]) as db:
        data = dict(attempt["data"])
        data.pop("provider_id")
        attempt = update(db, attempt, app.clock[0], data, "queued")
        emit(db, attempt["owner"], attempt["id"], "invoice.create", app.clock[0])
    return order, attempt


def test_tbank_worker_recovers_after_missing_order(app):
    from marina.operations import work_once

    order, attempt = queue_invoice(app)
    calls = []

    def fake(method, url, data, headers):
        operation = url.rsplit("/", 1)[-1]
        calls.append(operation)
        assert data["OrderId"] == attempt["id"]
        if calls == ["Init"]:
            raise TimeoutError("private upstream body")
        if operation == "CheckOrder":
            return {"Success": False, "ErrorCode": "335"}
        return {
            "Success": True,
            "PaymentId": "12345",
            "PaymentURL": "https://securepay.tinkoff.ru/fixture",
        }

    app.config["HTTP_TRANSPORT"] = fake
    assert work_once(app) == 1
    assert calls == ["Init"]
    app.clock[0] += 60
    assert work_once(app) == 1
    assert calls == ["Init", "CheckOrder", "Init"]
    with connect(app.config["DATABASE"]) as db:
        from marina import object_get

        saved = object_get(db, attempt["id"])
        assert saved["status"] == "ready"
        assert saved["data"]["provider_id"] == "12345"
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "lookup",
    [
        {"Success": True, "Payments": [{"PaymentId": "12345", "Status": "NEW"}]},
        {"Success": False, "ErrorCode": "9999"},
        {"Success": False, "ErrorCode": "335", "Payments": [{"PaymentId": "12345"}]},
    ],
)
def test_tbank_worker_never_recreates_unverified_order(app, lookup):
    from marina.operations import work_once

    _, attempt = queue_invoice(app)
    calls = []

    def fake(method, url, data, headers):
        operation = url.rsplit("/", 1)[-1]
        calls.append(operation)
        if operation == "Init":
            raise TimeoutError()
        return lookup

    app.config["HTTP_TRANSPORT"] = fake
    assert work_once(app) == 1
    for _ in range(4):
        app.clock[0] += 120
        work_once(app, send=lambda *args: None)
    assert calls.count("Init") == 1
    assert calls.count("CheckOrder") >= 1
    with connect(app.config["DATABASE"]) as db:
        from marina import object_get

        assert object_get(db, attempt["id"])["status"] == "uncertain"
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_tbank_expired_recovery_never_creates_invoice(app):
    from marina.operations import work_once
    from marina.db import update
    from marina import object_get

    order, attempt = queue_invoice(app)
    calls = []

    def fake(method, url, data, headers):
        calls.append(url.rsplit("/", 1)[-1])
        if calls == ["Init"]:
            raise TimeoutError()
        assert calls == ["Init", "CheckOrder"]
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, order["id"])
            update(db, current, app.clock[0], {**current["data"], "expires": app.clock[0]})
        return {"Success": False, "ErrorCode": "335"}

    app.config["HTTP_TRANSPORT"] = fake
    work_once(app)
    app.clock[0] += 60
    work_once(app)
    assert calls == ["Init", "CheckOrder"]
    with connect(app.config["DATABASE"]) as db:
        assert object_get(db, attempt["id"])["status"] == "uncertain"


def test_tbank_recovery_logs_safe_metadata_not_upstream_body(app, caplog):
    from marina.operations import work_once

    _, attempt = queue_invoice(app)

    def fake(*args):
        raise TimeoutError("do-not-log-this-secret")

    app.config["HTTP_TRANSPORT"] = fake
    work_once(app)
    assert "tbank operation=Init" in caplog.text
    assert attempt["id"] in caplog.text
    assert "TimeoutError" in caplog.text
    assert "do-not-log-this-secret" not in caplog.text
    assert "fixture-password" not in caplog.text


def test_tbank_recovery_preserves_known_payment_id(app):
    from marina.tbank import recover_invoice, TbankFailure

    settings(app)
    _, attempt = order_and_attempt(app)

    def unexpected(*args):
        pytest.fail("A known payment must not be recreated")

    app.config["HTTP_TRANSPORT"] = unexpected
    with pytest.raises(TbankFailure) as error:
        recover_invoice(app, attempt)
    assert not error.value.retryable
