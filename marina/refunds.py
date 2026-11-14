"""Supported full Stars refunds; partial or uncertain results require reconciliation."""

from flask import g
from . import body, object_get, protected, reason, require
from .commerce import policies
from .db import audit, emit, insert
from .screening import idempotent
from .providers import telegram


def refund_external(app, refund):
    require(refund["data"]["provider"] == "stars", "Этот возврат требует работы у провайдера")
    d = refund["data"]
    return telegram(
        app,
        "refundStarPayment",
        {"user_id": d["telegram_id"], "telegram_payment_charge_id": d["charge"]},
    )


def register_refunds(app):
    @app.post("/api/admin/orders/<id>/refund")
    @protected(admin=True)
    def request_refund(id):
        d = body()
        why = reason(d)
        order = object_get(g.db, id, "order")
        require(d.get("revision") == order["revision"], "Заказ изменён", 409)
        policy = policies(g.db, "commerce")
        require(
            policy and policy["data"].get("refund_policy_approved") is True,
            "Утвердите политику возвратов до запуска операции",
        )
        charge = d.get("charge")
        receipt = g.db.execute(
            "SELECT * FROM receipts WHERE order_id=? AND charge=?", (id, charge)
        ).fetchone()
        require(
            receipt and receipt["provider"] == "stars",
            "Автоматизирован только полный возврат Stars; Lava и частичный возврат требуют сверки",
        )
        require(
            d.get("amount") == receipt["amount"],
            "Частичный возврат этим маршрутом не поддерживается",
        )
        account = g.db.execute(
            "SELECT telegram_id FROM accounts WHERE id=?", (order["owner"],)
        ).fetchone()
        payload = {
            "order_id": id,
            "provider": "stars",
            "charge": receipt["charge"],
            "amount": receipt["amount"],
            "currency": receipt["currency"],
            "telegram_id": account["telegram_id"],
            "source_status": receipt["status"],
            "reason": why,
        }

        def create():
            obj = insert(g.db, "refund", payload, g.now, order["owner"], status="queued")
            emit(g.db, order["owner"], obj["id"], "refund.create", g.now)
            audit(g.db, g.actor["id"], "refund.request", obj, g.now, why)
            return obj

        return idempotent(g.db, "refund:stars", charge, payload, create), 202


def confirm_refund(db, payload, now):
    from .db import update, unpack

    order = object_get(db, payload["order_id"], "order")
    receipt = db.execute(
        "SELECT * FROM receipts WHERE provider=? AND charge=?", ("stars", payload["charge"])
    ).fetchone()
    require(receipt, "Поступление пока не сопоставлено; повторите сверку")
    require(
        receipt["order_id"] == order["id"]
        and receipt["amount"] == payload["amount"]
        and receipt["currency"] == payload["currency"],
        "Несоответствие возврата и поступления",
    )
    row = db.execute(
        "SELECT * FROM objects WHERE kind='refund' AND json_extract(data,'$.provider')='stars' AND json_extract(data,'$.charge')=?",
        (payload["charge"],),
    ).fetchone()
    if row:
        refund = unpack(row)
        if refund["status"] == "confirmed":
            return refund
        refund = update(db, refund, now, status="confirmed")
    else:
        refund = insert(
            db,
            "refund",
            {
                "order_id": order["id"],
                "provider": "stars",
                "charge": payload["charge"],
                "amount": payload["amount"],
                "currency": payload["currency"],
                "source": "authenticated_refunded_payment",
            },
            now,
            order["owner"],
            status="confirmed",
        )
    if receipt["status"] == "confirmed":
        for row in db.execute(
            "SELECT object_id FROM fulfillments WHERE order_id=?", (order["id"],)
        ).fetchall():
            obj = object_get(db, row["object_id"])
            if obj["kind"] == "right" and obj["status"] == "active":
                result = update(db, obj, now, status="revoked")
                audit(
                    db,
                    "provider:stars",
                    "right.refund",
                    result,
                    now,
                    "Подтверждён возврат исходного поступления",
                    obj["revision"],
                )
        if order["status"] != "refunded":
            update(db, order, now, {**order["data"], "refund_id": refund["id"]}, "refunded")
    return refund
