"""External boundaries. Credentials and upstream bodies are never logged."""

import hmac
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import g, request
from . import Problem, body, object_get, protected, reason, require
from .commerce import confirm
from .db import audit, emit, insert, transaction, unpack, update


def remote(app, method, url, data=None, headers=None, timeout=8):
    if app.config.get("HTTP_TRANSPORT"):
        return app.config["HTTP_TRANSPORT"](method, url, data, headers or {})
    req = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except (URLError, HTTPError, TimeoutError, ValueError) as error:
        # Includes neither URL with bot token nor upstream response body.
        raise Problem("Внешний сервис недоступен; операция сохранена для проверки", 503) from error


def telegram(app, method, data):
    require(app.config["BOT_TOKEN"], "Telegram не настроен", 503)
    markup = data.get("reply_markup")
    if isinstance(markup, dict):
        markup = dict(markup)
        for key in ("inline_keyboard", "keyboard"):
            if key in markup:
                markup[key] = [
                    [
                        {
                            **(button if isinstance(button, dict) else {"text": button}),
                            "style": "success",
                        }
                        for button in row
                    ]
                    for row in markup[key]
                ]
        data = {**data, "reply_markup": markup}
    result = remote(
        app,
        "POST",
        "https://api.telegram.org/bot" + app.config["BOT_TOKEN"] + "/" + method,
        data,
    )
    require(result.get("ok"), "Telegram отклонил запрос", 503)
    return result["result"]


def lava(app, method, path, data=None):
    key = app.config.get("LAVA_API_KEY")
    require(key, "Lava.top не настроена", 503)
    return remote(app, method, "https://gate.lava.top" + path, data, {"X-Api-Key": key})


def invoice(app, attempt):
    """Called only after the durable attempt has committed. Never auto-retry Lava POST."""
    d = attempt["data"]
    if d["provider"] == "stars":
        link = telegram(
            app,
            "createInvoiceLink",
            {
                "title": d["title"][:32],
                "description": d["terms"][:255],
                "payload": d["order_id"],
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": d["title"][:32], "amount": d["total"]}],
            },
        )
        return {"url": link, "provider_id": None}
    result = lava(
        app,
        "POST",
        "/api/v3/invoice",
        {
            "email": d["email"],
            "offerId": d["offer_id"],
            "currency": "RUB",
            "amount": d["total"] / 100,
            "periodicity": "ONE_TIME",
            "clientUtm": {"utm_source": attempt["id"]},
            "successful_return_url": app.config["PUBLIC_URL"] + "/?payment=return",
        },
    )
    require(
        isinstance(result.get("id"), str)
        and str(result.get("paymentUrl", "")).startswith("https://"),
        "Неопределённый ответ Lava.top; нужна сверка",
        503,
    )
    return {"url": result["paymentUrl"], "provider_id": result["id"]}


def check_lava(app, db, attempt, now):
    d = attempt["data"]
    require(d.get("provider_id"), "Сначала сопоставьте внешний счёт")
    remote_invoice = lava(app, "GET", "/api/v1/invoices/" + quote(d["provider_id"], safe=""))
    if remote_invoice.get("status") != "completed":
        return {"status": remote_invoice.get("status", "unknown")}
    receipt = remote_invoice.get("receipt", {})
    try:
        value = Decimal(str(receipt["amount"])) * 100
        require(value == value.to_integral_value(), "Некорректная точность суммы")
        paid_at = int(
            datetime.fromisoformat(remote_invoice["datetime"].replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise Problem("Неполное подтверждение Lava.top", 422)
    require(remote_invoice.get("id") == d["provider_id"], "Несоответствие внешнего счёта")
    order = object_get(db, d["order_id"], "order")
    return confirm(
        db,
        order,
        "lava",
        d["provider_id"],
        int(value),
        receipt.get("currency"),
        paid_at,
        now,
    )


def register_providers(app):
    app.config.setdefault("LAVA_API_KEY", os.getenv("LAVA_API_KEY", ""))
    app.config.setdefault("LAVA_WEBHOOK_SECRET", os.getenv("LAVA_WEBHOOK_SECRET", ""))

    @app.post("/api/orders/<id>/checkout")
    @protected()
    def checkout(id):
        o = object_get(g.db, id, "order", g.actor["id"])
        require(
            o["status"] == "pending" and o["data"]["expires"] > g.now,
            "Счёт уже оплачен или истёк",
        )
        row = g.db.execute(
            "SELECT * FROM objects WHERE kind='payment_attempt' AND json_extract(data,'$.order_id')=? ORDER BY created DESC LIMIT 1",
            (id,),
        ).fetchone()
        if row:
            return unpack(row)
        d = o["data"]
        request_data = body()
        product = d["product_snapshot"]
        payload = {
            "order_id": id,
            "provider": d["provider"],
            "currency": d["currency"],
            "total": d["total"],
            "title": product["title"],
            "terms": product["terms"],
        }
        if d["provider"] == "lava":
            require(
                app.config.get("LAVA_API_KEY") and app.config.get("LAVA_WEBHOOK_SECRET"),
                "Настройте Lava.top и webhook",
                503,
            )
            require(
                product.get("lava_offer_id") and app.config["PUBLIC_URL"].startswith("https://"),
                "Настройте offer ID и HTTPS-адрес",
            )
            email = request_data.get("email", "")
            require(
                isinstance(email, str) and "@" in email and len(email) <= 254,
                "Укажите email для чека",
            )
            payload.update(email=email, offer_id=product["lava_offer_id"])
        else:
            require(
                app.config["BOT_TOKEN"] and app.config["TELEGRAM_WEBHOOK_SECRET"],
                "Настройте Telegram и webhook",
                503,
            )
        attempt = insert(g.db, "payment_attempt", payload, g.now, g.actor["id"], status="queued")
        emit(g.db, g.actor["id"], attempt["id"], "invoice.create", g.now)
        return attempt, 202

    @app.post("/api/admin/payments/<id>/<action>")
    @protected(admin=True)
    def payment_action(id, action):
        d = body()
        why = reason(d)
        attempt = object_get(g.db, id, "payment_attempt")
        require(d.get("revision") == attempt["revision"], "Попытка изменена", 409)
        if action == "reconcile":
            require(
                attempt["data"]["provider"] == "lava",
                "Для Stars используйте журнал подтверждённых событий",
            )
            result = check_lava(app, g.db, attempt, g.now)
        elif action == "attach-invoice":
            require(
                attempt["data"]["provider"] == "lava" and attempt["status"] == "uncertain",
                "Сопоставление недоступно",
            )
            provider_id = d.get("provider_id")
            require(
                isinstance(provider_id, str) and 1 <= len(provider_id) < 200,
                "Укажите ID внешнего счёта",
            )
            result = update(
                g.db,
                attempt,
                g.now,
                {**attempt["data"], "provider_id": provider_id},
                "reconciled",
            )
            # Attaching an ID cannot grant access. The authenticated GET must confirm it.
        else:
            require(False, "Неизвестное действие", 404)
        audit(
            g.db,
            g.actor["id"],
            "payment." + action,
            attempt,
            g.now,
            why,
            attempt["revision"],
        )
        return result

    @app.post("/api/webhooks/lava")
    def lava_webhook():
        secret = app.config["LAVA_WEBHOOK_SECRET"]
        require(secret, "Webhook Lava.top не настроен", 503)
        require(
            hmac.compare_digest(request.headers.get("X-Api-Key", ""), secret),
            "Неверный ключ webhook",
            401,
        )
        d = body()
        if d.get("eventType") == "payment.success":
            from .commerce import record_payment_event, process_payment_event

            event = record_payment_event(
                app,
                "lava",
                d.get("contractId"),
                {"provider_id": d.get("contractId")},
                g.now,
            )
            try:
                process_payment_event(app, event["id"], g.now)
            except Exception:
                return {"ok": True, "queued": True}
            return {"ok": True}
        with transaction(app.config["DATABASE"]) as db:
            if d.get("event_type") in ("refund.success", "chargeback.initiated"):
                # Preserve authenticated financial evidence; policy-sensitive revocation is explicit.
                event_id = d.get("event_id")
                require(
                    isinstance(event_id, str) and len(event_id) < 200,
                    "Неверное событие",
                )
                from .screening import idempotent

                idempotent(
                    db,
                    "lava-adjustment",
                    event_id,
                    d,
                    lambda: insert(
                        db,
                        "reconciliation",
                        {"issue": d["event_type"], "financial_event": d},
                        g.now,
                        status="open",
                    ),
                )
        return {"ok": True}
