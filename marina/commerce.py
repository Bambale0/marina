"""Integer prices, immutable quotes and transactional fulfillment."""

import hmac
from decimal import Decimal, ROUND_HALF_UP

from flask import g, request
from . import body, object_get, offset, protected, reason, require
from .db import audit, connect, emit, insert, transaction, uid, unpack, update
from .screening import idempotent


def policies(db, type, scope=None):
    rows = db.execute(
        "SELECT * FROM objects WHERE kind='policy' AND status='published' ORDER BY created DESC,rowid DESC"
    )
    for row in rows:
        data = __import__("json").loads(row["data"])
        if data.get("type") != type:
            continue
        if scope is None and data.get("scope"):
            continue
        if scope is not None and data.get("scope") != scope:
            continue
        return unpack(row)
    return None


def rights(db, owner, subject=None):
    return [
        unpack(r)
        for r in db.execute(
            "SELECT * FROM objects WHERE kind='right' AND owner=? AND status='active' AND (? IS NULL OR subject=?)",
            (owner, subject, subject),
        )
    ]


def eligible_family(db, owner, selected, policy, now):
    ranks = {}
    for right in rights(db, owner):
        d = right["data"]
        if (
            d.get("service") == "subscription"
            and d["ends"] > now
            and (d["starts"] <= now or policy["include_future"])
        ):
            ranks[right["subject"]] = min(
                ranks.get(right["subject"], right["created"]), right["created"]
            )
    for s in selected:
        ranks.setdefault(s["id"], now)
    subjects = {s["id"]: s for s in selected}
    for id in ranks:
        subjects.setdefault(id, object_get(db, id, "subject", owner))
    return sorted(ranks, key=lambda id: (ranks[id], subjects[id]["created"], id))


def reconcile(db, order, now, issue, charge):
    obj = insert(
        db,
        "reconciliation",
        {
            "order_id": order["id"],
            "issue": issue,
            "charge": charge,
            "assigned_to": None,
        },
        now,
        order["owner"],
        status="open",
    )
    emit(db, order["owner"], obj["id"], "reconciliation.created", now)
    return obj


def confirm(db, order, provider, charge, amount, currency, paid_at, now, telegram_id=None):
    require(
        isinstance(charge, str)
        and 1 <= len(charge) <= 300
        and type(amount) is int
        and amount >= 0
        and type(paid_at) is int,
        "Неверное подтверждение платежа",
    )
    previous = db.execute(
        "SELECT * FROM receipts WHERE provider=? AND charge=?", (provider, charge)
    ).fetchone()
    if previous:
        require(
            previous["order_id"] == order["id"]
            and previous["amount"] == amount
            and previous["currency"] == currency,
            "Противоречивое повторное событие",
            409,
        )
        return order
    d = order["data"]
    mismatch = amount != d["total"] or currency != d["currency"] or provider != d["provider"]
    if telegram_id is not None:
        expected = db.execute(
            "SELECT telegram_id FROM accounts WHERE id=?", (order["owner"],)
        ).fetchone()[0]
        mismatch = mismatch or expected != telegram_id
    issue = (
        "amount_currency_or_owner_mismatch"
        if mismatch
        else "duplicate_payment"
        if order["status"] == "paid"
        or db.execute(
            "SELECT 1 FROM intent_fulfillments WHERE intent_id=?",
            (d.get("intent_id", order["id"]),),
        ).fetchone()
        else "late_payment"
        if paid_at > d["expires"]
        else None
    )
    if not issue and d.get("case_id"):
        existing_case = object_get(db, d["case_id"], "case", order["owner"])
        if existing_case["data"]["paid_at"] is not None:
            issue = "duplicate_payment"
    db.execute(
        "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)",
        (
            provider,
            charge,
            order["id"],
            amount,
            currency,
            paid_at,
            now,
            issue or "confirmed",
        ),
    )
    if issue:
        reconcile(db, order, now, issue, charge)
        if issue in ("duplicate_payment", "late_payment") and order["status"] != "paid":
            return update(db, order, now, status="paid_review")
        return order
    for line in d["lines"]:
        if db.execute(
            "SELECT 1 FROM fulfillments WHERE order_id=? AND line_id=?",
            (order["id"], line["id"]),
        ).fetchone():
            continue
        payload = {
            "order_id": order["id"],
            "line_id": line["id"],
            "product_id": d["product_id"],
            "product_snapshot": d["product_snapshot"],
            "paid_at": paid_at,
        }
        service = d["product_snapshot"]["service"]
        if service in ("subscription", "course"):
            start = max(paid_at, line.get("starts", paid_at))
            if service == "subscription":
                current = rights(db, order["owner"], line["subject_id"])
                start = max(
                    [start]
                    + [r["data"]["ends"] for r in current if r["data"].get("service") == service]
                )
            payload.update(
                service=service,
                starts=start,
                ends=start + d["product_snapshot"]["period_hours"] * 3600,
                course_id=d["product_snapshot"].get("course_id"),
            )
            obj = insert(db, "right", payload, now, order["owner"], line["subject_id"], "active")
        elif service == "pdf":
            payload.update(
                attempt_id=d["attempt_id"],
                due_at=paid_at + 48 * 3600,
                revisions=[],
                approved_revision=None,
                published_revision=None,
            )
            obj = insert(db, "report", payload, now, order["owner"], line["subject_id"])
        elif service == "guide":
            from .funnel import guide_paid

            payload.update(
                service="guide",
                material_id=d["material_id"],
                asset_id=d["asset_id"],
                run_id=d["run_id"],
            )
            obj = insert(db, "right", payload, now, order["owner"], status="active")
            guide_paid(db, order, obj, now)
        elif service == "case":
            payload.update(
                problem=d.get("problem", ""),
                submitted_at=None,
                due_at=None,
                answer=None,
                followup=None,
            )
            if d.get("case_id"):
                case = object_get(db, d["case_id"], "case", order["owner"])
                sent_at = case["data"]["submitted_at"]
                due_at = max(sent_at, paid_at) + 48 * 3600 if sent_at is not None else None
                obj = update(
                    db,
                    case,
                    now,
                    {
                        **case["data"],
                        **{
                            k: v
                            for k, v in payload.items()
                            if k in ("order_id", "line_id", "product_id", "paid_at")
                        },
                        "due_at": due_at,
                    },
                    "submitted" if sent_at is not None else "draft",
                )
                if sent_at is not None:
                    emit(db, obj["owner"], obj["id"], "case.submitted", now)
            else:
                obj = insert(db, "case", payload, now, order["owner"], line["subject_id"])
        elif service == "booking":
            obj = object_get(db, d.get("booking_id"), "booking", order["owner"])
            obj = update(
                db,
                obj,
                now,
                {**obj["data"], "order_id": order["id"], "paid_at": paid_at},
            )
        db.execute(
            "INSERT INTO intent_fulfillments VALUES(?,?,?,?)",
            (
                d.get("intent_id", order["id"]),
                line["subject_id"],
                order["id"],
                obj["id"],
            ),
        )
        db.execute(
            "INSERT INTO fulfillments VALUES(?,?,?)",
            (order["id"], line["id"], obj["id"]),
        )
        emit(db, order["owner"], obj["id"], "purchase.fulfilled", now)
    result = update(db, order, now, {**d, "paid_at": paid_at}, "paid")
    audit(
        db,
        "provider:" + provider,
        "payment.confirm",
        result,
        now,
        "Подтверждённое поступление",
        order["revision"],
    )
    return result


def record_payment_event(app, provider, external_id, payload, now):
    require(
        isinstance(external_id, str) and 1 <= len(external_id) <= 300,
        "Неверный идентификатор события",
    )
    with transaction(app.config["DATABASE"]) as db:
        owner = None
        if provider in ("stars", "stars_refund"):
            row = db.execute(
                "SELECT owner FROM objects WHERE id=? AND kind='order'",
                (payload.get("order_id"),),
            ).fetchone()
            owner = row["owner"] if row else None
        event = idempotent(
            db,
            "payment-event:" + provider,
            external_id,
            payload,
            lambda: insert(
                db,
                "payment_event",
                {"provider": provider, "payload": payload},
                now,
                owner,
                status="received",
            ),
        )
        marker = "payment-outbox:" + event["id"]
        if not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone():
            emit(db, owner or "", event["id"], "payment.process", now)
            db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))
        return event


def process_payment_event(app, event_id, now):
    with transaction(app.config["DATABASE"]) as db:
        event = object_get(db, event_id, "payment_event")
        if event["status"] == "processed":
            return
        data = event["data"]
        p = data["payload"]
        if data["provider"] == "stars_refund":
            from .refunds import confirm_refund

            confirm_refund(db, p, now)
        elif data["provider"] == "stars":
            order = object_get(db, p["order_id"], "order")
            confirm(
                db,
                order,
                "stars",
                p["charge"],
                p["amount"],
                p["currency"],
                p["paid_at"],
                now,
                p["telegram_id"],
            )
        else:
            from .providers import check_lava

            row = db.execute(
                "SELECT * FROM objects WHERE kind='payment_attempt' AND json_extract(data,'$.provider_id')=?",
                (p["provider_id"],),
            ).fetchone()
            if row:
                check_lava(app, db, unpack(row), now)
            else:
                insert(
                    db,
                    "reconciliation",
                    {
                        "issue": "unmatched_lava_invoice",
                        "provider_id": p["provider_id"],
                        "payment_event_id": event["id"],
                    },
                    now,
                    status="open",
                )
        update(db, event, now, status="processed")


def register_commerce(app):
    @app.post("/api/intents")
    @protected()
    def create_intent():
        d = body()
        require(
            d.get("service") in ("pdf", "case", "subscription", "course", "booking"),
            "Выберите услугу",
        )
        ids = d.get("subject_ids")
        require(
            isinstance(ids, list)
            and 1 <= len(ids) <= 20
            and all(isinstance(id, str) for id in ids)
            and len(set(ids)) == len(ids),
            "Выберите профили",
        )
        for id in ids:
            object_get(g.db, id, "subject", g.actor["id"])
        payload = {
            "service": d["service"],
            "subject_ids": sorted(ids),
            "reference": d.get("reference"),
        }
        return idempotent(
            g.db,
            "intent:" + g.actor["id"],
            d.get("key"),
            payload,
            lambda: insert(g.db, "purchase_intent", payload, g.now, g.actor["id"], status="open"),
        )

    @app.post("/api/quotes")
    @protected()
    def make_quote():
        d = body()
        p = object_get(g.db, d.get("product_id"), "product")
        require(p["status"] == "published", "Продукт не продаётся")
        ops = policies(g.db, "operations")
        require(
            not ops or not ops["data"].get("pause_sales"),
            "Приём новых заказов приостановлен",
        )
        if ops and ops["data"].get("max_open_orders"):
            count = g.db.execute(
                "SELECT count(*) FROM objects WHERE kind IN ('case','report') AND status NOT IN ('published','closed')"
            ).fetchone()[0]
            require(count < ops["data"]["max_open_orders"], "Лимит заказов достигнут")
        product = p["data"]
        if product["service"] == "guide":
            from .funnel import guide_quote

            return guide_quote(g.db, g.actor["id"], p, d, g.now), 201
        surface = d.get("surface")
        require(surface in ("telegram", "web"), "Укажите поверхность покупки")
        require(
            not (surface == "telegram" and product["currency"] != "XTR"),
            "Цифровые покупки в Telegram доступны через Stars",
        )
        require(
            surface != "web" or product.get("web_sales_approved") is True,
            "Веб-продажи пока не согласованы",
        )
        ids = d.get("subject_ids")
        require(
            isinstance(ids, list)
            and 1 <= len(ids) <= 20
            and all(isinstance(id, str) for id in ids)
            and len(set(ids)) == len(ids),
            "Выберите профили без повторов",
        )
        subjects = [object_get(g.db, id, "subject", g.actor["id"]) for id in ids]
        require(all(s["status"] == "active" for s in subjects), "Профиль архивирован")
        service = product["service"]
        require(service == "subscription" or len(subjects) == 1, "Выберите один профиль")
        policy = policies(g.db, "commerce")
        require(policy, "Сначала утвердите правила покупки в админке")
        rules = policy["data"]
        family = []
        if service == "subscription":
            require(
                all(s["data"]["kind"] == "child" for s in subjects),
                "Занятия предназначены детям",
            )
            family = eligible_family(g.db, g.actor["id"], subjects, rules, g.now)
        extra = {}
        reference = (
            d.get("attempt_id")
            if service == "pdf"
            else product.get("course_id")
            if service == "course"
            else d.get("booking_id")
            if service == "booking"
            else d.get("case_id")
            if service == "case"
            else None
        )
        if d.get("intent_id"):
            intent = object_get(g.db, d["intent_id"], "purchase_intent", g.actor["id"])
            require(
                intent["data"]
                == {
                    "service": service,
                    "subject_ids": sorted(ids),
                    "reference": reference,
                },
                "Намерение относится к другой покупке",
            )
            require(
                not g.db.execute(
                    "SELECT 1 FROM intent_fulfillments WHERE intent_id=?",
                    (intent["id"],),
                ).fetchone(),
                "Покупка уже исполнена",
                409,
            )
        else:
            intent = insert(
                g.db,
                "purchase_intent",
                {
                    "service": service,
                    "subject_ids": sorted(ids),
                    "reference": reference,
                },
                g.now,
                g.actor["id"],
                status="open",
            )
        if service == "pdf":
            attempt = object_get(g.db, d.get("attempt_id"), "attempt", g.actor["id"])
            require(
                attempt["status"] == "completed" and attempt["subject"] == subjects[0]["id"],
                "Нужен завершённый скрининг выбранного человека",
            )
            extra["attempt_id"] = attempt["id"]
        if service == "case" and d.get("case_id"):
            case = object_get(g.db, d["case_id"], "case", g.actor["id"])
            require(
                case["subject"] == subjects[0]["id"]
                and case["data"]["product_id"] == p["id"]
                and case["data"]["paid_at"] is None,
                "Комплект относится к другому тарифу, человеку или уже оплачен",
            )
            extra["case_id"] = case["id"]
        if service == "booking":
            booking = object_get(g.db, d.get("booking_id"), "booking", g.actor["id"])
            require(
                booking["status"] == "confirmed" and booking["subject"] == subjects[0]["id"],
                "Сначала подтвердите встречу",
            )
            extra["booking_id"] = booking["id"]
        if service == "course":
            course = object_get(g.db, product.get("course_id"), "course")
            require(course["status"] == "published", "Программа курса не опубликована")
            require(
                subjects[0]["data"]["kind"] == course["data"]["audience"],
                "Курс не подходит для этой аудитории",
            )
            from . import age_months

            require(
                course["data"]["min_months"]
                <= age_months(subjects[0]["data"]["birth_date"], g.now)
                <= course["data"]["max_months"],
                "Курс не подходит по возрасту",
            )
        lines = []
        for s in sorted(subjects, key=lambda s: family.index(s["id"]) if family else s["id"]):
            discount = [0, 3, 5, 8][min(family.index(s["id"]), 3)] if family else 0
            amount = int(
                (Decimal(product["amount"]) * Decimal(100 - discount) / 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            starts = g.now
            if service == "subscription":
                existing = [
                    r
                    for r in rights(g.db, g.actor["id"], s["id"])
                    if r["data"].get("service") == "subscription" and r["data"]["ends"] > g.now
                ]
                future = [r for r in existing if r["data"]["starts"] > g.now]
                require(
                    len(future) < rules["max_future_periods"],
                    "Достигнут лимит будущих периодов",
                )
                starts = max([starts] + [r["data"]["ends"] for r in existing])
                require(
                    starts - g.now <= rules["renewal_window_hours"] * 3600,
                    "Продление ещё недоступно",
                )
                trial = g.db.execute(
                    "SELECT * FROM trials WHERE account_id=?", (g.actor["id"],)
                ).fetchone()
                if trial and rules["trial_purchase"] == "after_trial":
                    starts = max(starts, trial["ends"])
            lines.append(
                {
                    "id": uid(),
                    "subject_id": s["id"],
                    "base": product["amount"],
                    "discount": discount,
                    "amount": amount,
                    "starts": starts,
                    "ends": starts + product.get("period_hours", 0) * 3600,
                }
            )
        data = {
            "intent_id": intent["id"],
            "product_id": p["id"],
            "product_snapshot": product,
            "policy_id": policy["id"],
            "policy_snapshot": rules,
            "family": family,
            "lines": lines,
            "total": sum(line["amount"] for line in lines),
            "currency": product["currency"],
            "provider": "stars" if product["currency"] == "XTR" else "lava",
            "surface": surface,
            "expires": g.now + rules["quote_ttl_seconds"],
            **extra,
        }
        quote = insert(g.db, "quote", data, g.now, g.actor["id"], status="valid")
        audit(g.db, g.actor["id"], "quote.create", quote, g.now, "Серверный расчёт покупки")
        return quote, 201

    @app.post("/api/orders")
    @protected()
    def create_order():
        d = body()
        q = object_get(g.db, d.get("quote_id"), "quote", g.actor["id"])

        def create():
            require(q["data"]["expires"] > g.now, "Расчёт истёк, обновите цену")
            require(
                not g.db.execute(
                    "SELECT 1 FROM objects WHERE kind='order' AND json_extract(data,'$.quote_id')=?",
                    (q["id"],),
                ).fetchone(),
                "Для расчёта уже создан заказ",
                409,
            )
            obj = insert(
                g.db,
                "order",
                {**q["data"], "quote_id": q["id"]},
                g.now,
                g.actor["id"],
                status="pending",
            )
            audit(g.db, g.actor["id"], "order.create", obj, g.now, "Создан заказ по снимку цены")
            return obj

        return idempotent(
            g.db, "order:" + g.actor["id"], d.get("key"), {"quote_id": q["id"]}, create
        )

    @app.get("/api/orders/<id>")
    @protected()
    def get_order(id):
        return object_get(g.db, id, "order", g.actor["id"])

    @app.get("/api/admin/objects")
    @protected(admin=True)
    def admin_objects():
        kind, owner, status = ((request.args.get(k) or None) for k in ("kind", "owner", "status"))
        return {
            "items": [
                unpack(r)
                for r in g.db.execute(
                    "SELECT * FROM objects WHERE (? IS NULL OR kind=?) AND (? IS NULL OR owner=?) AND (? IS NULL OR status=?) ORDER BY created,id LIMIT 100 OFFSET ?",
                    (kind, kind, owner, owner, status, status, offset()),
                )
            ]
        }

    @app.get("/api/admin/objects/<id>")
    @protected(admin=True)
    def admin_object(id):
        obj = object_get(g.db, id)
        receipts = [dict(r) for r in g.db.execute("SELECT * FROM receipts WHERE order_id=?", (id,))]
        deliveries = [
            dict(r) for r in g.db.execute("SELECT * FROM outbox WHERE object_id=?", (id,))
        ]
        return {
            "object": obj,
            "receipts": receipts,
            "deliveries": deliveries,
            "media": [
                dict(r)
                for r in g.db.execute("SELECT id,mime,created FROM blobs WHERE case_id=?", (id,))
            ],
            "fulfillments": [
                dict(r) for r in g.db.execute("SELECT * FROM fulfillments WHERE order_id=?", (id,))
            ],
        }

    @app.get("/api/admin/objects/<id>/versions")
    @protected(admin=True)
    def object_versions(id):
        import json

        object_get(g.db, id)
        return {
            "items": [
                {**dict(row), "data": json.loads(row["data"])}
                for row in g.db.execute(
                    "SELECT * FROM object_versions WHERE object_id=? ORDER BY revision",
                    (id,),
                )
            ]
        }

    @app.post("/api/admin/rights/grant")
    @protected(admin=True)
    def grant_right():
        d = body()
        why = reason(d)
        subject = object_get(g.db, d.get("subject_id"), "subject")
        require(d.get("service") in ("subscription", "course"), "Выберите вид доступа")
        require(
            type(d.get("starts")) is int and type(d.get("ends")) is int and d["ends"] > d["starts"],
            "Укажите начало и конец периода",
        )
        if d["service"] == "subscription":
            require(subject["data"]["kind"] == "child", "Занятия предназначены детям")
        if d["service"] == "course":
            object_get(g.db, d.get("course_id"), "course")
        payload = {
            "service": d["service"],
            "starts": d["starts"],
            "ends": d["ends"],
            "source": "compensation",
            "course_id": d.get("course_id"),
            "reason": why,
        }
        result = idempotent(
            g.db,
            "grant:" + g.actor["id"],
            d.get("key"),
            {**payload, "subject_id": subject["id"]},
            lambda: insert(
                g.db, "right", payload, g.now, subject["owner"], subject["id"], "active"
            ),
        )
        audit(g.db, g.actor["id"], "right.compensation", result, g.now, why)
        return result

    @app.post("/api/admin/rights/<id>/revoke")
    @protected(admin=True)
    def revoke_right(id):
        d = body()
        why = reason(d)
        obj = object_get(g.db, id, "right")
        require(d.get("revision") == obj["revision"], "Право изменено", 409)
        result = update(g.db, obj, g.now, status="revoked")
        audit(g.db, g.actor["id"], "right.revoke", result, g.now, why, obj["revision"])
        return result

    @app.post("/api/admin/reconciliations/<id>/resolve")
    @protected(admin=True)
    def resolve_reconciliation(id):
        d = body()
        why = reason(d)
        obj = object_get(g.db, id, "reconciliation")
        require(
            obj["status"] == "open" and obj["revision"] == d.get("revision"),
            "Задача уже изменена",
            409,
        )
        result = update(
            g.db,
            obj,
            g.now,
            {**obj["data"], "resolution": why, "resolved_by": g.actor["id"]},
            "resolved",
        )
        audit(
            g.db,
            g.actor["id"],
            "reconciliation.resolve",
            result,
            g.now,
            why,
            obj["revision"],
        )
        return result

    @app.post("/api/webhooks/telegram")
    def telegram_webhook():
        secret = app.config["TELEGRAM_WEBHOOK_SECRET"]
        require(secret, "Webhook ещё не настроен", 503)
        require(
            hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret),
            "Неверная подпись webhook",
            401,
        )
        d = body()
        message = d.get("message", {})
        if "refunded_payment" in message:
            p = message["refunded_payment"]
            payload = {
                "order_id": p.get("invoice_payload"),
                "charge": p.get("telegram_payment_charge_id"),
                "amount": p.get("total_amount"),
                "currency": p.get("currency"),
            }
            event = record_payment_event(app, "stars_refund", payload["charge"], payload, g.now)
            try:
                process_payment_event(app, event["id"], g.now)
            except Exception:
                return {"ok": True, "queued": True}
            return {"ok": True}
        if "successful_payment" in message:
            p = message["successful_payment"]
            payload = {
                "order_id": p.get("invoice_payload"),
                "charge": p.get("telegram_payment_charge_id"),
                "amount": p.get("total_amount"),
                "currency": p.get("currency"),
                "paid_at": int(message.get("date", g.now)),
                "telegram_id": message.get("from", {}).get("id"),
            }
            event = record_payment_event(app, "stars", payload["charge"], payload, g.now)
            try:
                process_payment_event(app, event["id"], g.now)
            except Exception:
                return {"ok": True, "queued": True}
            return {"ok": True}
        with connect(app.config["DATABASE"]) as db:
            if "pre_checkout_query" in d:
                from .providers import telegram

                p = d["pre_checkout_query"]
                row = db.execute(
                    "SELECT * FROM objects WHERE id=? AND kind='order'",
                    (p.get("invoice_payload"),),
                ).fetchone()
                o = unpack(row)
                expected = (
                    db.execute(
                        "SELECT telegram_id FROM accounts WHERE id=?", (o["owner"],)
                    ).fetchone()[0]
                    if o
                    else None
                )
                ok = bool(
                    o
                    and expected == p.get("from", {}).get("id")
                    and o["status"] == "pending"
                    and o["data"]["expires"] > g.now
                    and o["data"]["total"] == p.get("total_amount")
                    and p.get("currency") == "XTR"
                )
                telegram(
                    app,
                    "answerPreCheckoutQuery",
                    {
                        "pre_checkout_query_id": p["id"],
                        "ok": ok,
                        **(
                            {} if ok else {"error_message": "Счёт недействителен. Обновите расчёт."}
                        ),
                    },
                )
            elif message.get("text", "").startswith("/start"):
                from .providers import telegram

                require(
                    app.config["PUBLIC_URL"].startswith("https://"),
                    "Настройте HTTPS-адрес приложения",
                    503,
                )
                telegram(
                    app,
                    "sendMessage",
                    {
                        "chat_id": message["chat"]["id"],
                        "text": "Нейроразвитие дома. Откройте кабинет, чтобы выбрать профиль и продолжить.",
                        "reply_markup": {
                            "inline_keyboard": [
                                [
                                    {
                                        "text": "Открыть кабинет",
                                        "web_app": {"url": app.config["PUBLIC_URL"]},
                                    }
                                ]
                            ]
                        },
                    },
                )
        return {"ok": True}
