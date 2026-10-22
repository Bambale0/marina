import os
from pathlib import Path

from flask import g
from . import body, object_get, protected, reason, require
from .db import audit, connect, emit, insert, transaction, unpack, update


def backup(source, target):
    require(Path(source).is_file(), "Исходная БД не найдена")
    require(not Path(target).exists(), "Файл резервной копии уже существует")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with connect(source) as src, connect(target) as dst:
        src.backup(dst)
    return verify_backup(target)


def verify_backup(path):
    require(Path(path).is_file(), "Резервная копия не найдена")
    with connect(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        require(
            integrity == "ok" and not db.execute("PRAGMA foreign_key_check").fetchall(),
            "Резервная копия повреждена",
        )
        return {
            "integrity": integrity,
            "accounts": db.execute("SELECT count(*) FROM accounts").fetchone()[0],
            "objects": db.execute("SELECT count(*) FROM objects").fetchone()[0],
            "files": db.execute("SELECT count(*) FROM blobs").fetchone()[0],
        }


def notify(app, owner, object_id, event):
    from .providers import telegram

    with connect(app.config["DATABASE"]) as db:
        account = db.execute("SELECT telegram_id FROM accounts WHERE id=?", (owner,)).fetchone()
    if event.startswith("sla.") or event in (
        "case.submitted",
        "case.followup",
        "booking.requested",
        "crm.video_received",
        "crm.contact_due",
        "lesson.review_required",
    ):
        require(app.config["ADMIN_TELEGRAM_IDS"], "Укажите специалиста для уведомлений")
        for id in app.config["ADMIN_TELEGRAM_IDS"]:
            telegram(
                app,
                "sendMessage",
                {
                    "chat_id": id,
                    "text": "Есть новое обращение или срок обслуживания. Откройте /queue. Личные данные не включены в уведомление.",
                },
            )
        return
    if event == "subscription.reminder":
        from .commerce import rights

        with connect(app.config["DATABASE"]) as db:
            right = object_get(db, object_id, "right")
            if right["status"] != "active" or any(
                r["data"].get("service") == "subscription"
                and r["data"]["ends"] > right["data"]["ends"]
                for r in rights(db, owner, right["subject"])
            ):
                return
        telegram(
            app,
            "sendMessage",
            {
                "chat_id": account["telegram_id"],
                "text": "Срок занятий подходит к концу. Для продления выберите ребёнка и оформите новую оплату. Автоматического списания не будет.",
            },
        )
        return
    if event == "crm.message":
        from .crm import notify_crm

        return notify_crm(app, owner, object_id, event)
    if event.startswith("booking."):
        with connect(app.config["DATABASE"]) as db:
            booking = object_get(db, object_id, "booking", owner)
        if booking["data"].get("review_id"):
            from .crm import notify_crm

            return notify_crm(app, owner, object_id, event)
    require(account, "Получатель не найден")
    telegram(
        app,
        "sendMessage",
        {
            "chat_id": account["telegram_id"],
            "text": "В кабинете обновлён статус вашего обращения. Откройте приложение, чтобы продолжить.",
        },
    )


def work_once(app, send=None):
    now = int(app.config["CLOCK"]())
    path = app.config["DATABASE"]
    with transaction(path) as db:
        row = db.execute(
            "SELECT * FROM outbox WHERE (status='pending' AND available<=?) OR (status='processing' AND leased_until<=?) ORDER BY created,id LIMIT 1",
            (now, now),
        ).fetchone()
        if not row:
            return 0
        event = dict(row)
        # If an invoice creation lease expired, the remote POST may have succeeded.
        uncertain = event["status"] == "processing" and event["event"] in (
            "invoice.create",
            "refund.create",
        )
        db.execute(
            "UPDATE outbox SET status='processing',attempts=attempts+1,leased_until=? WHERE id=?",
            (now + 60, event["id"]),
        )
    outcome, result = "sent", None
    try:
        if event["event"] == "invoice.create":
            from .providers import invoice

            require(not uncertain, "Нужна сверка незавершённой попытки")
            with connect(path) as db:
                attempt = object_get(db, event["object_id"], "payment_attempt")
            result = invoice(app, attempt)
        elif event["event"] == "refund.create":
            from .refunds import refund_external

            require(not uncertain, "Требуется сверка незавершённого возврата")
            with connect(path) as db:
                refund = object_get(db, event["object_id"], "refund")
            result = refund_external(app, refund)
        elif event["event"] == "payment.process":
            from .commerce import process_payment_event

            process_payment_event(app, event["object_id"], now)
        elif event["event"] == "ai.generate":
            from .ai import generate

            with connect(path) as db:
                job = object_get(db, event["object_id"], "ai_job")
            result = generate(app, job)
        else:
            (send or notify)(app, event["owner"], event["object_id"], event["event"])
    except Exception:
        # No upstream exception text: it can contain credentials or client content.
        outcome = "failed"
    with transaction(path) as db:
        current = db.execute("SELECT * FROM outbox WHERE id=?", (event["id"],)).fetchone()
        if current["status"] != "processing" or current["leased_until"] != now + 60:
            return 1
        db.execute(
            "INSERT INTO deliveries(event_id,at,outcome) VALUES(?,?,?)",
            (event["id"], now, outcome),
        )
        if event["event"] == "invoice.create":
            attempt = object_get(db, event["object_id"], "payment_attempt")
            update(
                db,
                attempt,
                now,
                {**attempt["data"], **(result or {})},
                "ready" if outcome == "sent" else "uncertain",
            )
            state = "sent" if outcome == "sent" else "failed"
            if outcome == "failed":
                insert(
                    db,
                    "reconciliation",
                    {
                        "issue": "invoice_creation_uncertain",
                        "attempt_id": attempt["id"],
                    },
                    now,
                    attempt["owner"],
                    status="open",
                )
        elif event["event"] == "refund.create":
            refund = object_get(db, event["object_id"], "refund")
            state = (
                "sent"
                if refund["status"] == "confirmed" or (outcome == "sent" and result is True)
                else "failed"
            )
            if refund["status"] != "confirmed":
                update(db, refund, now, status="confirmed" if state == "sent" else "uncertain")
            if state == "sent":
                order = object_get(db, refund["data"]["order_id"], "order")
                if refund["data"]["source_status"] == "confirmed":
                    for row in db.execute(
                        "SELECT object_id FROM fulfillments WHERE order_id=?", (order["id"],)
                    ).fetchall():
                        obj = object_get(db, row["object_id"])
                        if obj["kind"] == "right":
                            revoked = update(db, obj, now, status="revoked")
                            audit(
                                db,
                                "provider:stars",
                                "right.refund",
                                revoked,
                                now,
                                "Подтверждён полный возврат исходной оплаты",
                                obj["revision"],
                            )
                    update(db, order, now, {**order["data"], "refund_id": refund["id"]}, "refunded")
            else:
                insert(
                    db,
                    "reconciliation",
                    {
                        "issue": "refund_uncertain",
                        "refund_id": refund["id"],
                        "order_id": refund["data"]["order_id"],
                    },
                    now,
                    refund["owner"],
                    status="open",
                )
        elif event["event"] == "ai.generate":
            job = object_get(db, event["object_id"], "ai_job")
            report = object_get(db, job["data"]["report_id"], "report")
            state = "sent" if outcome == "sent" else "failed"
            if outcome == "sent" and report["revision"] == job["data"]["report_revision"]:
                revisions = [
                    *report["data"]["revisions"],
                    {
                        "number": len(report["data"]["revisions"]) + 1,
                        "text": result,
                        "actor": "ai",
                        "at": now,
                        "job_id": job["id"],
                    },
                ]
                update(
                    db,
                    report,
                    now,
                    {
                        **report["data"],
                        "revisions": revisions,
                        "approved_revision": None,
                    },
                    "draft",
                )
                update(db, job, now, {**job["data"], "output": result}, "completed")
            elif outcome == "sent":
                update(db, job, now, {**job["data"], "output": result}, "conflict")
            else:
                update(db, job, now, status="failed")
        else:
            state = (
                "sent" if outcome == "sent" else "failed" if current["attempts"] >= 8 else "pending"
            )
        if event["event"] == "crm.message":
            from .crm import message_delivery

            message_delivery(db, event, now, outcome == "sent")
        db.execute(
            "UPDATE outbox SET status=?,available=?,leased_until=NULL WHERE id=?",
            (state, now + min(3600, 2 ** current["attempts"] * 10), event["id"]),
        )
    return 1


def tick(app):
    now = int(app.config["CLOCK"]())
    with transaction(app.config["DATABASE"]) as db:
        for row in db.execute(
            "SELECT * FROM objects WHERE kind='video_review' AND status NOT IN ('closed','completed') "
            "AND json_extract(data,'$.next_contact')<=?",
            (now,),
        ).fetchall():
            review = unpack(row)
            marker = f"crm-contact:{review['id']}:{review['data']['next_contact']}"
            if not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone():
                emit(db, review["owner"], review["id"], "crm.contact_due", now)
                db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))
        for row in db.execute(
            "SELECT * FROM objects WHERE kind='case' AND status='answered' AND json_extract(data,'$.followup_until')<=?",
            (now,),
        ).fetchall():
            obj = unpack(row)
            if obj["data"].get("followup") is None:
                update(db, obj, now, status="closed")
        for row in db.execute(
            "SELECT * FROM objects WHERE kind='media' AND status='ready' AND json_extract(data,'$.expires')<=?",
            (now,),
        ).fetchall():
            media = unpack(row)
            parent = media["data"].get("review_id") or media["data"].get("case_id")
            case = object_get(db, parent)
            marker = "retention:" + media["id"]
            if (
                case["status"] in ("closed", "completed")
                and not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone()
            ):
                insert(
                    db,
                    "retention_task",
                    {"media_id": media["id"], "case_id": case["id"]},
                    now,
                    media["owner"],
                    media["subject"],
                    "pending",
                )
                db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))
        from .commerce import policies

        settings = policies(db, "operations")
        d = settings["data"] if settings else {}
        for row in db.execute(
            "SELECT * FROM objects WHERE kind IN ('report','case') AND status NOT IN ('published','closed')"
        ).fetchall():
            obj = unpack(row)
            due = (
                obj["data"].get("followup", {}).get("due_at")
                if obj["status"] == "followup_pending"
                else obj["data"].get("due_at")
            )
            if not due:
                continue
            for hours in d.get("sla_reminder_hours", []):
                if now >= due - hours * 3600:
                    marker = "sla:" + obj["id"] + ":" + str(due) + ":" + str(hours)
                    if not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone():
                        emit(
                            db,
                            obj["owner"],
                            obj["id"],
                            "sla.overdue" if now >= due else "sla.upcoming",
                            now,
                        )
                        db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))
        for row in db.execute(
            "SELECT * FROM objects WHERE kind='right' AND status='active'"
        ).fetchall():
            obj = unpack(row)
            if obj["data"].get("service") != "subscription":
                continue
            for hours in d.get("renewal_reminder_hours", []):
                if obj["data"]["ends"] - hours * 3600 <= now < obj["data"]["ends"]:
                    marker = "renew:" + obj["id"] + ":" + str(hours)
                    if not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone():
                        emit(db, obj["owner"], obj["id"], "subscription.reminder", now)
                        db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))


def register_operations(app):
    @app.get("/api/admin/operations")
    @protected(admin=True)
    def operations():
        counts = [
            dict(r)
            for r in g.db.execute(
                "SELECT kind,status,count(*) AS count FROM objects GROUP BY kind,status"
            )
        ]
        outbox = [
            dict(r) for r in g.db.execute("SELECT * FROM outbox ORDER BY created DESC LIMIT 100")
        ]
        return {
            "metrics": counts,
            "outbox": outbox,
            "integrations": {
                k: bool(app.config.get(k))
                for k in (
                    "BOT_TOKEN",
                    "TELEGRAM_WEBHOOK_SECRET",
                    "LAVA_API_KEY",
                    "LAVA_WEBHOOK_SECRET",
                    "OPENAI_API_KEY",
                )
            },
            "now": g.now,
        }

    @app.get("/api/admin/queue")
    @protected(admin=True)
    def queue():
        rows = [
            unpack(r)
            for r in g.db.execute(
                "SELECT * FROM objects WHERE kind IN ('video_review','report','case','booking','reconciliation','data_request','ai_job','refund','payment_event','retention_task') AND status NOT IN ('published','closed','completed','cancelled','resolved','processed','confirmed') ORDER BY created,id LIMIT 200"
            )
        ]
        for obj in rows:
            due = (
                obj["data"].get("next_contact")
                if obj["kind"] == "video_review"
                else obj["data"].get("followup", {}).get("due_at")
                if obj["status"] == "followup_pending"
                else obj["data"].get("due_at")
            )
            obj.update(due_at=due, overdue=bool(due and due < g.now))
        return {"items": sorted(rows, key=lambda o: o["due_at"] or 2**60)}

    @app.post("/api/admin/outbox/<id>/retry")
    @protected(admin=True)
    def retry_delivery(id):
        why = reason(body())
        row = g.db.execute("SELECT * FROM outbox WHERE id=?", (id,)).fetchone()
        require(
            row and row["event"] not in ("invoice.create", "refund.create"),
            "Повтор создания счёта требует сверки",
            422,
        )
        require(row["status"] in ("failed", "sent"), "Доставка уже обрабатывается", 409)
        g.db.execute(
            "UPDATE outbox SET status='pending',available=?,leased_until=NULL WHERE id=?",
            (g.now, id),
        )
        audit(g.db, g.actor["id"], "outbox.retry", {"id": id}, g.now, why)
        return {"ok": True}

    @app.post("/api/admin/jobs/<id>/cancel")
    @protected(admin=True)
    def cancel_job(id):
        d = body()
        why = reason(d)
        job = object_get(g.db, id, "ai_job")
        require(
            job["status"] == "queued" and d.get("revision") == job["revision"],
            "Задание уже изменено",
            409,
        )
        require(
            not g.db.execute(
                "SELECT 1 FROM outbox WHERE object_id=? AND status='processing'", (id,)
            ).fetchone(),
            "Задание уже выполняется",
            409,
        )
        g.db.execute(
            "UPDATE outbox SET status='cancelled' WHERE object_id=? AND status='pending'",
            (id,),
        )
        result = update(g.db, job, g.now, status="cancelled")
        audit(g.db, g.actor["id"], "ai.cancel", result, g.now, why, job["revision"])
        return result

    @app.get("/api/admin/outbox/<id>/attempts")
    @protected(admin=True)
    def delivery_attempts(id):
        return {
            "items": [
                dict(r)
                for r in g.db.execute(
                    "SELECT * FROM deliveries WHERE event_id=? ORDER BY id", (id,)
                )
            ]
        }
