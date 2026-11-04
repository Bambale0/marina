"""Age-only consultation quotes using the existing Lava commerce pipeline."""

import copy

from flask import g

from . import body, object_get, protected, require
from .commerce import policies, provider_enabled
from .db import audit, insert, uid, unpack
from .screening import idempotent


def consultation_product(db, run):
    """Resolve a legacy run's missing product from its currently published age funnel."""
    from .funnel import published

    current = published(db, run["data"]["child_age"])
    product_id = run["data"]["snapshot"].get("consultation_product_id")
    if not product_id:
        product_id = current["data"].get("consultation_product_id") if current else None
    require(product_id, "Тариф консультации для этого возраста ещё не опубликован")
    product = object_get(db, product_id, "product")
    data = product["data"]
    require(
        product["status"] == "published"
        and data.get("service") == "booking"
        and data.get("currency") == "RUB"
        and data.get("age_group") == run["data"]["child_age"]
        and type(data.get("amount")) is int
        and data["amount"] > 0,
        "Выберите опубликованный рублёвый тариф консультации для возраста ребёнка",
    )
    require(
        data.get("web_sales_approved") is True
        and isinstance(data.get("lava_offer_id"), str)
        and bool(data["lava_offer_id"].strip()),
        "Для консультации настройте отдельный offer ID Lava.top и разрешение продаж",
    )
    guide_ids = {run["data"]["snapshot"].get("guide_product_id")}
    if current:
        guide_ids.add(current["data"].get("guide_product_id"))
    for guide_id in guide_ids - {None, ""}:
        guide = object_get(db, guide_id, "product")
        require(
            guide["data"].get("lava_offer_id") != data["lava_offer_id"],
            "Для консультации нужен отдельный offer Lava.top, отличный от руководства",
        )
    return product


def register_consultation_commerce(app):
    """Register the consultation quote API."""

    @app.post("/api/funnel-bookings/<id>/quote")
    @protected()
    def consultation_quote(id):
        body()  # The request carries no trusted price, product, coupon or ownership fields.
        booking = object_get(g.db, id, "booking", g.actor["id"])
        require(not booking["data"].get("paid_at"), "Консультация уже оплачена", 409)
        require(booking["status"] == "confirmed", "Сначала подтвердите время консультации")
        run = object_get(g.db, booking["data"].get("run_id"), "funnel_run", g.actor["id"])
        require(run["status"] == "completed", "Скрининг не завершён")
        require(provider_enabled(app, "lava"), "Этот способ оплаты отключён")
        operations = policies(g.db, "operations")
        require(
            not operations or not operations["data"].get("pause_sales"),
            "Приём новых заказов приостановлен",
        )
        product = consultation_product(g.db, run)
        intent = idempotent(
            g.db,
            "consultation-intent:" + g.actor["id"],
            booking["id"],
            {},
            lambda: insert(
                g.db,
                "purchase_intent",
                {"service": "booking", "subject_ids": [booking["id"]], "reference": booking["id"]},
                g.now,
                g.actor["id"],
                status="open",
            ),
        )
        require(
            not g.db.execute(
                "SELECT 1 FROM intent_fulfillments WHERE intent_id=?", (intent["id"],)
            ).fetchone(),
            "Консультация уже оплачена",
            409,
        )
        for row in g.db.execute(
            "SELECT * FROM objects WHERE kind='quote' AND owner=? "
            "AND json_extract(data,'$.intent_id')=? ORDER BY created DESC,rowid DESC",
            (g.actor["id"], intent["id"]),
        ):
            existing = unpack(row)
            if existing["status"] == "valid" and existing["data"]["expires"] > g.now:
                return existing
        policy = policies(g.db, "commerce", "consultation")
        require(policy, "Опубликуйте правила покупки консультации")
        rules = policy["data"]
        require(
            type(rules.get("quote_ttl_seconds")) is int
            and rules["quote_ttl_seconds"] > 0
            and rules.get("late_payment") == "review",
            "Проверьте срок расчёта и правила поздней оплаты консультации",
        )
        row = g.db.execute(
            "SELECT * FROM objects WHERE kind='template' AND status='published' "
            "AND json_extract(data,'$.type')='offer' ORDER BY updated DESC,rowid DESC LIMIT 1"
        ).fetchone()
        offer = unpack(row) if row else None
        require(
            offer and offer["id"] == rules.get("offer_template_id"),
            "Привяжите правила консультации к текущей опубликованной оферте",
        )
        promotion = copy.deepcopy(booking["data"].get("promotion", {}))
        regular = promotion.get("regular_amount_rub")
        base = regular * 100 if type(regular) is int and regular > 0 else product["data"]["amount"]
        total = base
        if promotion.get("eligible") is True:
            amount = promotion.get("amount_rub")
            require(type(amount) is int and amount > 0, "Проверьте сохранённую цену консультации")
            total = amount * 100
        payload = {
            "intent_id": intent["id"],
            "product_id": product["id"],
            "product_snapshot": copy.deepcopy(product["data"]),
            "policy_id": policy["id"],
            "policy_snapshot": copy.deepcopy(rules),
            "offer_id": offer["id"],
            "offer_snapshot": copy.deepcopy(offer["data"]),
            "booking_id": booking["id"],
            "booking_snapshot": copy.deepcopy(booking),
            "consultation_run_id": run["id"],
            "age_group": run["data"]["child_age"],
            "promotion_snapshot": promotion,
            "family": [],
            "lines": [
                {
                    "id": uid(),
                    "subject_id": booking["id"],
                    "base": base,
                    "discount": (base - total) * 100 // base,
                    "amount": total,
                }
            ],
            "total": total,
            "currency": "RUB",
            "provider": "lava",
            "surface": "telegram",
            "expires": g.now + rules["quote_ttl_seconds"],
        }
        quote = insert(g.db, "quote", payload, g.now, g.actor["id"], status="valid")
        audit(
            g.db,
            g.actor["id"],
            "quote.create",
            quote,
            g.now,
            "Серверная цена подтверждённой консультации по сохранённым условиям заявки",
        )
        return quote, 201
