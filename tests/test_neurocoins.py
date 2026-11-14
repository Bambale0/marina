"""Neurocoin wallet: no client funds are credited before a verified Lava receipt."""

import concurrent.futures

import pytest

from marina import Problem
from marina.commerce import confirm
from marina.db import connect, insert, transaction, unpack
from marina.neurocoins import entry, validate_policy
from tests.test_identity import app as identity_app
from tests.test_identity import login


@pytest.fixture
def app(tmp_path):
    return identity_app.__wrapped__(tmp_path)


@pytest.fixture
def client_and_admin(app):
    app.config.update(
        NEUROCOINS_LAVA_ENABLED=True,
        LAVA_API_KEY="fixture-key",
        LAVA_WEBHOOK_SECRET="fixture-secret",
        PUBLIC_URL="https://fixture.example",
        ENABLED_PAYMENT_PROVIDERS=["lava"],
    )
    return login(app, 2), login(app, 1)


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
        "services": [
            {"id": "online_lesson", "title": "Занятие", "coins": price}
        ],
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
        json={"package_id": "p150", "key": key, "booking_id": booking_id,
              "surface": surface},
    )


def confirmed_receipt(app, order_id, *, charge="lava-charge-fixture", amount=15000):
    with transaction(app.config["DATABASE"]) as db:
        order = unpack(db.execute("SELECT * FROM objects WHERE id=?", (order_id,)).fetchone())
        return confirm(
            db, order, "lava", charge, amount, "RUB",
            app.clock[0], app.clock[0],
        )


def wallet(client):
    response = client.get("/api/wallet")
    assert response.status_code == 200, response.json
    return response.json


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


def test_policy_rejects_stars_and_invalid_amounts(app):
    valid = {
        "type": "neurocoins", "currency": "RUB", "enabled": True,
        "terms": "Только услуги", "quote_ttl_seconds": 3600,
        "packages": [{"id": "p150", "title": "150 НК", "coins": 150,
                      "lava_offer_id": "fixture"}],
        "services": [{"id": "online_lesson", "title": "Занятие", "coins": 120}],
    }
    validate_policy(valid)
    with pytest.raises(Problem):
        validate_policy({**valid, "currency": "XTR"})
    with pytest.raises(Problem):
        validate_policy({**valid, "packages": [
            {"id": "p150", "title": "Stars", "coins": 150, "stars": 150}]})
    with pytest.raises(Problem):
        validate_policy({**valid, "packages": [
            {"id": "p150", "title": "Пакет", "coins": True, "lava_offer_id": "fixture"}]})
    with pytest.raises(Problem):
        validate_policy({**valid, "services": [
            {"id": "online_lesson", "title": "Пакет", "coins": -1}]})


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
    assert admin.post(
        "/api/admin/wallet/" + person + "/adjust",
        json={**base, "key": "adjust-456"},
    ).status_code == 403
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
            "AND json_extract(data,'$.booking_id')=?", (booking["id"],)
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
        "admin_adjustment": 1, "booking": 1, "booking_refund": 1
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


def test_lava_topup_is_external_only_and_never_credits_before_receipt(
    app, client_and_admin
):
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
        assert db.execute(
            "SELECT count(*) FROM objects WHERE kind='order'"
        ).fetchone()[0] == 1


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
        ledger = list(db.execute(
            "SELECT source,delta FROM neurocoin_ledger ORDER BY created,id"
        ))
    assert sorted((r["source"], r["delta"]) for r in ledger) == [
        ("booking", -120), ("lava_credit", 150)
    ]


def test_cancelled_booking_preserves_lava_funds_without_auto_purchase(
    app, client_and_admin
):
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
        assert db.execute(
            "SELECT count(*) FROM neurocoin_ledger WHERE source='booking'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM objects WHERE kind='reconciliation' "
            "AND json_extract(data,'$.issue')='neurocoins_booking_cancelled'"
        ).fetchone()[0] == 0


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
