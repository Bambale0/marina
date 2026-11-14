"""Neurocoins: an internal, non-transferable service balance backed by an append-only ledger.

No exchange rate is built into code. Star-pack and lesson prices are controlled by
an approved/published neurocoins policy. Coin credits follow confirmed receipts only.
"""

import json
import re

from flask import g

from . import body, object_get, protected, reason, require
from .commerce import policies, provider_enabled
from .db import audit, emit, insert, uid, update
from .screening import idempotent

MAX_ENTRY = 1_000_000_000
PACK_ID = re.compile(r"[a-z][a-z0-9_-]{1,47}\Z")


def validate_policy(d):
    require(type(d.get("enabled")) is bool, "Укажите статус кошелька")
    require(isinstance(d.get("terms"), str) and bool(d["terms"].strip()), "Укажите условия Нейро-коинов")
    require(
        type(d.get("quote_ttl_seconds")) is int
        and 60 <= d["quote_ttl_seconds"] <= 604800,
        "Укажите срок действия счёта",
    )
    require(isinstance(d.get("packages"), list) and len(d["packages"]) <= 50, "Проверьте пакеты")
    require(isinstance(d.get("services"), list) and len(d["services"]) <= 50, "Проверьте тарифы")
    seen = set()
    for pack in d["packages"]:
        require(
            isinstance(pack, dict)
            and isinstance(pack.get("id"), str)
            and PACK_ID.fullmatch(pack["id"])
            and pack["id"] not in seen
            and isinstance(pack.get("title"), str)
            and 1 <= len(pack["title"].strip()) <= 100
            and type(pack.get("coins")) is int
            and 1 <= pack["coins"] <= MAX_ENTRY
            and type(pack.get("stars")) is int
            and 1 <= pack["stars"] <= MAX_ENTRY,
            "Проверьте ID, монеты и цену пакета в Stars",
        )
        seen.add(pack["id"])
    seen.clear()
    for item in d["services"]:
        require(
            isinstance(item, dict)
            and item.get("id") == "online_lesson"
            and item["id"] not in seen
            and isinstance(item.get("title"), str)
            and 1 <= len(item["title"].strip()) <= 100
            and type(item.get("coins")) is int
            and 1 <= item["coins"] <= MAX_ENTRY,
            "Проверьте стоимость онлайн-занятия в Нейро-коинах",
        )
        seen.add(item["id"])


def active_policy(db):
    p = policies(db, "neurocoins")
    return p if p and p["data"].get("enabled") is True else None


def balance(db, owner):
    row = db.execute(
        "SELECT COALESCE(SUM(delta),0) FROM neurocoin_ledger WHERE owner=?", (owner,)
    ).fetchone()
    return int(row[0])


def entry(db, owner, delta, source, reference, now, actor, why, *, allow_negative=False):
    """Append exactly once per source/reference; all callers hold BEGIN IMMEDIATE."""
    require(
        type(delta) is int and delta != 0 and abs(delta) <= MAX_ENTRY,
        "Некорректное количество Нейро-коинов",
    )
    require(
        isinstance(reference, str) and 0 < len(reference) <= 300,
        "Некорректный идентификатор операции",
    )
    row = db.execute(
        "SELECT * FROM neurocoin_ledger WHERE source=? AND reference=?",
        (source, reference),
    ).fetchone()
    if row:
        require(
            row["owner"] == owner and row["delta"] == delta,
            "Повторная операция противоречит исходной",
            409,
        )
        return dict(row)
    current = balance(db, owner)
    require(allow_negative or current + delta >= 0, "Недостаточно Нейро-коинов", 409)
    require(abs(current + delta) < 10**13, "Лимит баланса превышен", 409)
    result = {
        "id": uid(),
        "owner": owner,
        "delta": delta,
        "source": source,
        "reference": reference,
        "created": now,
    }
    db.execute(
        "INSERT INTO neurocoin_ledger(id,owner,delta,source,reference,created) "
        "VALUES(:id,:owner,:delta,:source,:reference,:created)",
        result,
    )
    audit(db, actor, "neurocoins." + source, {"id": result["id"], "revision": 1}, now, why)
    return result


def fulfill_topup(db, order, paid_at, now):
    """Called only by the existing fulfill() transaction after verified Stars receipt."""
    d = order["data"]
    require(
        d.get("provider") == "stars"
        and d.get("currency") == "XTR"
        and isinstance(d.get("coin_pack"), dict)
        and type(d["coin_pack"].get("coins")) is int
        and d["coin_pack"]["coins"] > 0,
        "Некорректный оплаченный пакет",
    )
    receipt = db.execute(
        "SELECT charge FROM receipts WHERE order_id=? AND provider='stars' "
        "AND status='confirmed' AND amount=? AND currency='XTR'",
        (order["id"], d["total"]),
    ).fetchone()
    require(receipt, "Нейро-коины начисляются только после подтверждения Stars", 409)
    movement = entry(
        db,
        order["owner"],
        d["coin_pack"]["coins"],
        "stars_credit",
        order["id"],
        now,
        "provider:stars",
        "Подтверждённый платёж Stars за пакет Нейро-коинов",
    )
    return insert(
        db,
        "neurocoin_credit",
        {
            "order_id": order["id"],
            "ledger_id": movement["id"],
            "coins": d["coin_pack"]["coins"],
            "paid_at": paid_at,
        },
        now,
        order["owner"],
        status="recorded",
    )


def refund_topup(db, order, charge, now):
    if order["data"].get("product_snapshot", {}).get("service") != "neurocoins":
        return
    pack = order["data"]["coin_pack"]
    entry(
        db,
        order["owner"],
        -pack["coins"],
        "stars_refund",
        charge,
        now,
        "provider:stars",
        "Подтверждён возврат платежа Stars; неиспользованные монеты списаны",
        allow_negative=True,
    )


def refund_booking(db, booking, now, actor, why):
    if booking["data"].get("payment_source") != "neurocoins":
        return
    coins = booking["data"].get("neurocoins_paid")
    require(type(coins) is int and coins > 0, "Некорректная запись оплаты", 409)
    entry(
        db,
        booking["owner"],
        coins,
        "booking_refund",
        booking["id"],
        now,
        actor,
        why,
    )


def register_neurocoins(app):
    @app.get("/api/wallet")
    @protected()
    def wallet():
        policy = active_policy(g.db)
        history = g.db.execute(
            "SELECT id,delta,source,reference,created FROM neurocoin_ledger "
            "WHERE owner=? ORDER BY created DESC,id DESC LIMIT 50",
            (g.actor["id"],),
        ).fetchall()
        return {
            "currency": "NEURO",
            "balance": balance(g.db, g.actor["id"]),
            "enabled": bool(policy),
            "packages": policy["data"]["packages"] if policy else [],
            "services": policy["data"]["services"] if policy else [],
            "history": [dict(x) for x in history],
        }

    @app.post("/api/wallet/topups")
    @protected()
    def topup():
        d = body()
        require(provider_enabled(app, "stars"), "Пополнение через Stars пока не подключено", 503)
        require(
            app.config["BOT_TOKEN"] and app.config.get("TELEGRAM_WEBHOOK_SECRET"),
            "Приём платежей Stars не настроен",
            503,
        )
        p = active_policy(g.db)
        require(p, "Пополнение Нейро-коинов пока недоступно", 409)
        ops = policies(g.db, "operations")
        require(not ops or not ops["data"].get("pause_sales"), "Продажи приостановлены", 409)
        pack = next(
            (x for x in p["data"]["packages"] if x["id"] == d.get("package_id")),
            None,
        )
        require(pack, "Выберите пакет из опубликованного списка", 404)
        require(
            isinstance(d.get("key"), str) and 4 <= len(d["key"]) <= 150,
            "Укажите уникальный ключ покупки",
        )
        snapshot = dict(pack)

        def create():
            order = insert(
                g.db,
                "order",
                {
                    "intent_id": uid(),
                    "product_id": "neurocoins:" + pack["id"],
                    "product_snapshot": {
                        "service": "neurocoins",
                        "title": pack["title"],
                        "terms": p["data"]["terms"],
                    },
                    "policy_id": p["id"],
                    "coin_pack": snapshot,
                    "lines": [
                        {
                            "id": uid(),
                            "subject_id": g.actor["id"],
                            "amount": pack["stars"],
                        }
                    ],
                    "total": pack["stars"],
                    "currency": "XTR",
                    "provider": "stars",
                    "surface": "telegram",
                    "expires": g.now + p["data"]["quote_ttl_seconds"],
                },
                g.now,
                g.actor["id"],
                status="pending",
            )
            audit(
                g.db,
                g.actor["id"],
                "neurocoins.topup_order",
                order,
                g.now,
                "Запрос пакета Нейро-коинов с оплатой Telegram Stars",
            )
            return order

        return idempotent(
            g.db,
            "neurocoins-topup:" + g.actor["id"],
            d["key"],
            {"package_id": pack["id"], "coins": pack["coins"], "stars": pack["stars"]},
            create,
        ), 201

    @app.post("/api/wallet/bookings/<id>/pay")
    @protected()
    def pay_booking(id):
        d = body()
        require(
            isinstance(d.get("key"), str) and 4 <= len(d["key"]) <= 150,
            "Укажите ключ операции",
        )
        p = active_policy(g.db)
        require(p, "Оплата Нейро-коинами пока не включена", 409)
        service = next(
            (item for item in p["data"]["services"] if item["id"] == "online_lesson"),
            None,
        )
        require(service, "Тариф онлайн-занятия пока не опубликован", 409)
        booking = object_get(g.db, id, "booking", g.actor["id"])
        require(
            booking["status"] == "confirmed"
            and booking["data"].get("booking_kind") == "online_lesson"
            and booking["data"].get("paid_at") is None
            and not g.db.execute(
                "SELECT 1 FROM objects WHERE kind='order' "
                "AND json_extract(data,'$.booking_id')=? "
                "AND status IN ('pending','paid','paid_review')",
                (id,),
            ).fetchone(),
            "Запись не подтверждена, уже оплачена или имеет незакрытый счёт",
            409,
        )

        def create():
            movement = entry(
                g.db,
                g.actor["id"],
                -service["coins"],
                "booking",
                booking["id"],
                g.now,
                g.actor["id"],
                "Оплата подтверждённого онлайн-занятия Нейро-коинами",
            )
            result = update(
                g.db,
                booking,
                g.now,
                {
                    **booking["data"],
                    "paid_at": g.now,
                    "payment_source": "neurocoins",
                    "neurocoins_paid": service["coins"],
                    "neurocoins_tariff": dict(service),
                    "neurocoins_policy_id": p["id"],
                    "neurocoins_entry_id": movement["id"],
                },
            )
            audit(
                g.db,
                g.actor["id"],
                "booking.neurocoins_payment",
                result,
                g.now,
                "Оплачено из внутреннего баланса",
                booking["revision"],
            )
            emit(g.db, result["owner"], result["id"], "booking.paid", g.now)
            return {"booking_id": id, "balance": balance(g.db, g.actor["id"])}

        return idempotent(
            g.db,
            "neurocoins-booking:" + g.actor["id"],
            d["key"],
            {"booking_id": id, "price": service["coins"], "policy_id": p["id"]},
            create,
        )

    @app.post("/api/admin/wallet/<owner>/adjust")
    @protected(admin=True)
    def adjust(owner):
        d = body()
        why = reason(d)
        require(
            g.actor["role"] == "admin"
            and g.actor["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"],
            "Права специалиста отозваны",
            403,
        )
        account = g.db.execute("SELECT blocked FROM accounts WHERE id=?", (owner,)).fetchone()
        require(account and not account["blocked"], "Клиент недоступен", 404)
        delta = d.get("delta")
        require(type(delta) is int and delta != 0 and abs(delta) <= MAX_ENTRY, "Неверная сумма")
        key = d.get("key")
        require(isinstance(key, str) and 4 <= len(key) <= 150, "Нужен уникальный ключ")
        movement = entry(
            g.db,
            owner,
            delta,
            "admin_adjustment",
            owner + ":" + key,
            g.now,
            g.actor["id"],
            why,
        )
        return {"entry": movement, "balance": balance(g.db, owner)}
