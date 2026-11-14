"""Neurocoins: an internal, non-transferable service balance backed by an append-only ledger.

One internal coin is one RUB. Package sizes, service prices and Lava offer IDs
are published via an approved policy. Only verified provider receipts mint coins.
"""

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
    require(d.get("currency") == "RUB", "Один Нейро-коин соответствует одному рублю")
    require(
        isinstance(d.get("terms"), str) and bool(d["terms"].strip()), "Укажите условия Нейро-коинов"
    )
    require(
        type(d.get("quote_ttl_seconds")) is int and 60 <= d["quote_ttl_seconds"] <= 604800,
        "Укажите срок действия счёта",
    )
    require(isinstance(d.get("packages"), list) and len(d["packages"]) <= 50, "Проверьте пакеты")
    require(isinstance(d.get("services"), list) and len(d["services"]) <= 50, "Проверьте тарифы")
    seen = set()
    for pack in d["packages"]:
        require(
            isinstance(pack, dict)
            and set(pack) == {"id", "title", "coins", "lava_offer_id"}
            and isinstance(pack.get("id"), str)
            and PACK_ID.fullmatch(pack["id"])
            and pack["id"] not in seen
            and isinstance(pack.get("title"), str)
            and 1 <= len(pack["title"].strip()) <= 100
            and type(pack.get("coins")) is int
            and 1 <= pack["coins"] <= MAX_ENTRY
            and isinstance(pack.get("lava_offer_id"), str)
            and 1 <= len(pack["lava_offer_id"]) <= 200,
            "Проверьте название, номинал и Lava offer ID пакета",
        )
        seen.add(pack["id"])
    seen.clear()
    for item in d["services"]:
        require(
            isinstance(item, dict)
            and set(item) == {"id", "title", "coins"}
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


def payable_booking(db, owner, booking_id):
    booking = object_get(db, booking_id, "booking", owner)
    require(
        booking["status"] == "confirmed"
        and booking["data"].get("booking_kind") == "online_lesson"
        and booking["data"].get("paid_at") is None
        and not db.execute(
            "SELECT 1 FROM objects WHERE kind='order' "
            "AND json_extract(data,'$.booking_id')=? "
            "AND status IN ('pending','paid','paid_review')",
            (booking_id,),
        ).fetchone(),
        "Занятие уже оплачено, отменено или имеет незакрытый счёт",
        409,
    )
    return booking


def spend_booking(db, booking, price, policy_id, now, actor):
    """Atomic debit + paid booking; one debit per booking, regardless of callers."""
    require(type(price) is int and 0 < price <= MAX_ENTRY, "Некорректный тариф")
    booking = payable_booking(db, booking["owner"], booking["id"])
    movement = entry(
        db,
        booking["owner"],
        -price,
        "booking",
        booking["id"],
        now,
        actor,
        "Оплата занятия Нейро-коинами",
    )
    result = update(
        db,
        booking,
        now,
        {
            **booking["data"],
            "paid_at": now,
            "payment_source": "neurocoins",
            "neurocoins_paid": price,
            "neurocoins_policy_id": policy_id,
            "neurocoins_entry_id": movement["id"],
        },
    )
    audit(
        db,
        actor,
        "booking.neurocoins_payment",
        result,
        now,
        "Оплачено внутренними монетами",
        booking["revision"],
    )
    emit(db, result["owner"], result["id"], "booking.paid", now)
    return result


def fulfill_topup(db, order, paid_at, now):
    """The provider receipt was inserted by confirm() in this same transaction."""
    d = order["data"]
    pack = d.get("coin_pack")
    require(
        d.get("provider") == "lava"
        and d.get("currency") == "RUB"
        and isinstance(pack, dict)
        and type(pack.get("coins")) is int
        and 0 < pack["coins"] <= MAX_ENTRY
        and d.get("total") == pack["coins"] * 100,
        "Параметры пополнения не совпадают со снимком заказа",
    )
    receipt = db.execute(
        "SELECT charge FROM receipts WHERE order_id=? AND provider='lava' "
        "AND status='confirmed' AND amount=? AND currency='RUB'",
        (order["id"], d["total"]),
    ).fetchone()
    require(receipt, "Монеты начисляются только после подтверждения Lava.top", 409)
    movement = entry(
        db,
        order["owner"],
        pack["coins"],
        "lava_credit",
        order["id"],
        now,
        "provider:lava",
        "Подтверждённое поступление Lava.top",
    )
    credit = insert(
        db,
        "neurocoin_credit",
        {
            "order_id": order["id"],
            "ledger_id": movement["id"],
            "coins": pack["coins"],
            "paid_at": paid_at,
        },
        now,
        order["owner"],
        status="recorded",
    )
    auto_id = d.get("auto_booking_id")
    if auto_id and order["status"] != "pending":
        audit(
            db,
            "provider:lava",
            "neurocoins.auto_purchase_skipped",
            credit,
            now,
            "Пополнение подтверждено, но исходный счёт уже не активен",
        )
    elif auto_id:
        # Successful top-up is independent of the booking. A cancelled or
        # already paid booking simply leaves the coins on the user's balance.
        from . import Problem

        try:
            booking = payable_booking(db, order["owner"], auto_id)
            spend_booking(
                db,
                booking,
                d["auto_price_coins"],
                d["auto_policy_id"],
                now,
                "provider:lava",
            )
        except Problem as exc:
            audit(
                db,
                "provider:lava",
                "neurocoins.auto_purchase_skipped",
                credit,
                now,
                "Зачисление сохранено; покупка не состоялась: " + exc.message,
            )
    return credit


def refund_topup(db, order, charge, now):
    """Call only after a *verified* Lava refund/chargeback event or reconciliation."""
    if order["data"].get("product_snapshot", {}).get("service") != "neurocoins":
        return
    pack = order["data"]["coin_pack"]
    entry(
        db,
        order["owner"],
        -pack["coins"],
        "lava_refund",
        charge,
        now,
        "provider:lava",
        "Подтверждённый возврат Lava.top",
        allow_negative=True,
    )


def refund_booking(db, booking, now, actor, why):
    """Cancellation does not mint coins until a specialist approves a refund."""
    if booking["data"].get("payment_source") != "neurocoins":
        return None
    existing = db.execute(
        "SELECT * FROM objects WHERE kind='reconciliation' "
        "AND json_extract(data,'$.issue')='neurocoins_booking_cancelled' "
        "AND json_extract(data,'$.booking_id')=?",
        (booking["id"],),
    ).fetchone()
    if existing:
        return existing["id"]
    coins = booking["data"].get("neurocoins_paid")
    require(type(coins) is int and coins > 0, "Некорректная запись оплаты", 409)
    issue = insert(
        db,
        "reconciliation",
        {
            "issue": "neurocoins_booking_cancelled",
            "booking_id": booking["id"],
            "coins": coins,
            "reason": why,
        },
        now,
        booking["owner"],
        status="open",
    )
    audit(db, actor, "neurocoins.refund_review", issue, now, why)
    emit(db, booking["owner"], issue["id"], "reconciliation.created", now)
    return issue["id"]


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
            "spend_enabled": bool(policy)
            and app.config.get("NEUROCOINS_BOT_SPEND_ENABLED") is True,
            "packages": policy["data"]["packages"] if policy else [],
            "services": policy["data"]["services"] if policy else [],
            "history": [dict(x) for x in history],
        }

    @app.get("/api/wallet/topups/<id>")
    @protected()
    def topup_status(id):
        """Private status polling for the independently hosted checkout page."""
        order = object_get(g.db, id, "order", g.actor["id"])
        require(
            order["data"].get("product_snapshot", {}).get("service") == "neurocoins",
            "Пополнение не найдено",
            404,
        )
        row = g.db.execute(
            "SELECT * FROM objects WHERE kind='payment_attempt' AND owner=? "
            "AND json_extract(data,'$.order_id')=? ORDER BY created DESC LIMIT 1",
            (g.actor["id"], id),
        ).fetchone()
        from .db import unpack

        attempt = unpack(row)
        from .commerce import checkout_open

        link = (
            attempt["data"].get("url")
            if (
                attempt
                and attempt["status"] == "ready"
                and checkout_open(g.db, order, g.now)
                and app.config.get("NEUROCOINS_LAVA_ENABLED") is True
            )
            else None
        )
        return {
            "order_id": id,
            "status": order["status"],
            "attempt_status": attempt["status"] if attempt else None,
            "payment_url": link if isinstance(link, str) and link.startswith("https://") else None,
            "balance": balance(g.db, g.actor["id"]),
            "auto_booking_id": order["data"].get("auto_booking_id"),
        }

    @app.post("/api/wallet/topups")
    @protected()
    def topup():
        d = body()
        # Only standalone, independently compliance-reviewed web checkout.
        # The Telegram bot must never initiate Lava payment for digital goods.
        require(
            app.config.get("NEUROCOINS_LAVA_ENABLED") is True
            and d.get("surface") == "external_web",
            "Оплата Lava.top недоступна в Telegram",
            403,
        )
        require(provider_enabled(app, "lava"), "Приём платежей Lava.top отключён", 503)
        require(
            app.config.get("LAVA_API_KEY")
            and app.config.get("LAVA_WEBHOOK_SECRET")
            and app.config.get("PUBLIC_URL", "").startswith("https://"),
            "Lava.top или HTTPS не настроены",
            503,
        )
        p = active_policy(g.db)
        require(p, "Пополнение монет пока не опубликовано", 409)
        ops = policies(g.db, "operations")
        require(not ops or not ops["data"].get("pause_sales"), "Продажи приостановлены", 409)
        pack = next(
            (x for x in p["data"]["packages"] if x["id"] == d.get("package_id")),
            None,
        )
        require(pack, "Пакет отсутствует в опубликованном тарифе", 404)
        key = d.get("key")
        require(isinstance(key, str) and 4 <= len(key) <= 120, "Нужен уникальный ключ")
        booking_id = d.get("booking_id")
        previous = g.db.execute(
            "SELECT object_id FROM dedup WHERE scope=? AND key=?",
            ("neurocoins-topup:" + g.actor["id"], key),
        ).fetchone()
        if previous:
            existing = object_get(g.db, previous["object_id"], "order", g.actor["id"])
            require(
                existing["data"].get("coin_pack", {}).get("id") == d.get("package_id")
                and existing["data"].get("auto_booking_id") == booking_id,
                "Ключ уже использован для другого пополнения",
                409,
            )
            return existing, 201
        auto_price = None
        if booking_id is not None:
            payable_booking(g.db, g.actor["id"], booking_id)
            service = next(
                (x for x in p["data"]["services"] if x["id"] == "online_lesson"),
                None,
            )
            require(service, "Тариф занятия не опубликован", 409)
            auto_price = service["coins"]
            require(
                not g.db.execute(
                    "SELECT 1 FROM objects WHERE kind='order' "
                    "AND json_extract(data,'$.auto_booking_id')=? "
                    "AND status IN ('pending','paid','paid_review')",
                    (booking_id,),
                ).fetchone(),
                "Для занятия уже открыт счёт на пополнение",
                409,
            )
            require(balance(g.db, g.actor["id"]) < auto_price, "Баланс уже достаточен")
            require(
                balance(g.db, g.actor["id"]) + pack["coins"] >= auto_price,
                "Выберите пакет, покрывающий недостающую сумму",
            )
        snapshot = dict(pack)
        offer = pack["lava_offer_id"]

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
                        "lava_offer_id": offer,
                    },
                    "policy_id": p["id"],
                    "coin_pack": snapshot,
                    "lines": [
                        {"id": uid(), "subject_id": g.actor["id"], "amount": pack["coins"] * 100}
                    ],
                    "total": pack["coins"] * 100,
                    "currency": "RUB",
                    "provider": "lava",
                    "surface": "external_web",
                    "auto_booking_id": booking_id,
                    "auto_price_coins": auto_price,
                    "auto_policy_id": p["id"] if booking_id else None,
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
                "Пополнение Нейро-коинов через Lava.top",
            )
            return order

        return idempotent(
            g.db,
            "neurocoins-topup:" + g.actor["id"],
            key,
            {
                "package_id": pack["id"],
                "coins": pack["coins"],
                "offer": offer,
                "booking_id": booking_id,
                "price": auto_price,
            },
            create,
        ), 201

    @app.post("/api/wallet/bookings/<id>/pay")
    @protected()
    def pay_booking(id):
        require(
            app.config.get("NEUROCOINS_BOT_SPEND_ENABLED") is True,
            "Оплата Нейро-коинами внутри Telegram пока не разрешена",
            403,
        )
        d = body()
        key = d.get("key")
        require(isinstance(key, str) and 4 <= len(key) <= 120, "Укажите ключ операции")
        booking = object_get(g.db, id, "booking", g.actor["id"])
        if booking["data"].get("payment_source") == "neurocoins":
            return {"booking_id": id, "balance": balance(g.db, g.actor["id"])}
        p = active_policy(g.db)
        require(p, "Оплата Нейро-коинами пока не включена", 409)
        service = next(
            (x for x in p["data"]["services"] if x["id"] == "online_lesson"),
            None,
        )
        require(service, "Тариф занятия не опубликован", 409)
        booking = payable_booking(g.db, g.actor["id"], id)
        paid = spend_booking(g.db, booking, service["coins"], p["id"], g.now, g.actor["id"])
        return {"booking_id": paid["id"], "balance": balance(g.db, g.actor["id"])}

    @app.post("/api/admin/wallet/bookings/<id>/refund")
    @protected(admin=True)
    def approve_booking_refund(id):
        d = body()
        why = reason(d)
        require(
            g.actor["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"],
            "Права специалиста отозваны",
            403,
        )
        booking = object_get(g.db, id, "booking")
        require(
            booking["status"] == "cancelled"
            and booking["data"].get("payment_source") == "neurocoins",
            "Только отменённая запись, оплаченная Нейро-коинами",
            409,
        )
        amount = booking["data"].get("neurocoins_paid")
        require(type(amount) is int and amount > 0, "Некорректная сумма возврата", 409)
        issue_row = g.db.execute(
            "SELECT * FROM objects WHERE kind='reconciliation' "
            "AND json_extract(data,'$.issue')='neurocoins_booking_cancelled' "
            "AND json_extract(data,'$.booking_id')=?",
            (id,),
        ).fetchone()
        require(issue_row, "Задача сверки возврата не создана", 409)
        from .db import unpack

        issue = unpack(issue_row)
        movement = entry(
            g.db,
            booking["owner"],
            amount,
            "booking_refund",
            id,
            g.now,
            g.actor["id"],
            why,
        )
        if issue["status"] != "resolved":
            resolved = update(
                g.db,
                issue,
                g.now,
                {
                    **issue["data"],
                    "resolution": why,
                    "resolved_by": g.actor["id"],
                    "ledger_id": movement["id"],
                },
                "resolved",
            )
            audit(
                g.db,
                g.actor["id"],
                "neurocoins.refund_approved",
                resolved,
                g.now,
                why,
                issue["revision"],
            )
        return {"entry": movement, "balance": balance(g.db, booking["owner"])}

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
