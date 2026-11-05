"""Track specialist-authored consultation results; never generate clinical content."""

import copy

from flask import Response, g

from . import body, object_get, protected, reason, require
from .attachments import response_attachments
from .db import audit, connect, emit, insert, transaction, update


def validate_result_delivery(spec):
    require(isinstance(spec, dict), "Настройте персональный результат консультации")
    for key in ("version", "title", "filename"):
        require(
            isinstance(spec.get(key), str) and 0 < len(spec[key].strip()) <= 200,
            "Укажите версию, название и имя файла персонального результата",
        )
    require(spec.get("format") == "pdf", "Персональный письменный результат выдаётся в PDF")
    require(
        spec["filename"].lower().endswith(".pdf")
        and not any(char in spec["filename"] for char in ("/", "\\", "\r", "\n", '"')),
        "Укажите безопасное имя PDF без пути",
    )
    hours = spec.get("due_hours")
    require(
        "due_hours" in spec and (hours is None or (type(hours) is int and hours > 0)),
        "Срок результата задаётся в часах или остаётся неуказанным",
    )
    require(
        type(spec.get("source_page")) is int and spec["source_page"] > 0,
        "Укажите страницу авторского источника результата",
    )


def create_consultation_result(db, booking, now, actor, why):
    """Create one obligation in the same transaction as the recorded completion."""
    require(booking["status"] == "completed", "Сначала отметьте состоявшуюся консультацию")
    if booking["data"].get("result_id"):
        existing = object_get(
            db, booking["data"]["result_id"], "consultation_result", booking["owner"]
        )
        require(
            existing["data"]["booking_id"] == booking["id"], "Результат относится к другой встрече"
        )
        return booking
    journey = booking["data"].get("journey_snapshot", {})
    spec = journey.get("result_delivery")
    source = {"journey_version": journey.get("version"), "booking_revision": booking["revision"]}
    if not spec and booking["data"].get("run_id"):
        # Old booked journeys predate structured result rules. Adopt only approved current rules.
        from .funnel import published

        run = object_get(db, booking["data"]["run_id"], "funnel_run", booking["owner"])
        current = published(db, run["data"]["child_age"])
        if current:
            current_journey = current["data"].get("consultation_journey", {})
            spec = current_journey.get("result_delivery")
            source = {
                "journey_version": current_journey.get("version"),
                "funnel_id": current["id"],
                "funnel_revision": current["revision"],
                "legacy_booking": True,
            }
    if not spec:
        # Preserve unrelated legacy appointments that never promised a configured written result.
        return booking
    validate_result_delivery(spec)
    completed_at = booking["data"]["completed_at"]
    hours = spec["due_hours"]
    result = insert(
        db,
        "consultation_result",
        {
            "booking_id": booking["id"],
            "title": spec["title"],
            "filename": spec["filename"],
            "result_delivery": copy.deepcopy(spec),
            "source": source,
            "completed_at": completed_at,
            "due_at": completed_at + hours * 3600 if hours is not None else None,
            "answer_attachments": [],
            "published_at": None,
            "delivered_at": None,
        },
        now,
        booking["owner"],
        booking["subject"],
        "in_progress",
    )
    audit(db, actor, "consultation_result.create", result, now, why)
    emit(db, result["owner"], result["id"], "consultation_result.created", now)
    return update(db, booking, now, {**booking["data"], "result_id": result["id"]})


def published_pdf(db, result):
    ids = result["data"].get("answer_attachments", [])
    require(
        result["status"] in ("delivery_pending", "completed")
        and result["data"].get("published_at") is not None
        and len(ids) == 1,
        "Специалист ещё не опубликовал персональный PDF",
        404,
    )
    media = object_get(db, ids[0], "media", result["owner"])
    require(
        media["status"] == "ready"
        and media["data"].get("case_id") == result["id"]
        and media["data"].get("purpose") == "specialist_response",
        "Файл результата недоступен",
        404,
    )
    blob = db.execute(
        "SELECT body FROM blobs WHERE id=? AND owner=? AND case_id=? AND mime='application/pdf'",
        (ids[0], result["owner"], result["id"]),
    ).fetchone()
    require(blob, "Файл результата недоступен по правилам хранения", 404)
    return blob["body"]


def notify_consultation_result(app, owner, id, event):
    if event == "consultation_result.created":
        from .consultation_delivery import send_once
        from .providers import telegram

        require(app.config["ADMIN_TELEGRAM_IDS"], "Укажите специалиста для уведомлений")
        for chat in sorted(app.config["ADMIN_TELEGRAM_IDS"]):
            send_once(
                app,
                "consultation-result:" + id + ":created:" + str(chat),
                lambda chat=chat: telegram(
                    app,
                    "sendMessage",
                    {
                        "chat_id": chat,
                        "text": "После состоявшейся консультации нужно подготовить персональный письменный результат. Откройте /queue: там указаны документ и срок.",
                    },
                ),
            )
        return
    require(event == "consultation_result.published", "Неизвестное событие результата")
    from .bot import send_file

    with connect(app.config["DATABASE"]) as db:
        result = object_get(db, id, "consultation_result", owner)
        if result["status"] == "completed":
            return
        account = db.execute(
            "SELECT telegram_id,blocked FROM accounts WHERE id=?", (owner,)
        ).fetchone()
        require(account and not account["blocked"], "Доставка получателю пока недоступна")
        pdf = published_pdf(db, result)
    send_file(app, account["telegram_id"], result["data"]["filename"], pdf, "application/pdf")
    # Only an acknowledged send changes the obligation to completed. Outbox retries exceptions.
    now = int(app.config["CLOCK"]())
    with transaction(app.config["DATABASE"]) as db:
        current = object_get(db, id, "consultation_result", owner)
        if current["status"] == "completed":
            return
        require(
            current["status"] == "delivery_pending"
            and current["data"]["answer_attachments"] == result["data"]["answer_attachments"],
            "Опубликованный результат изменён",
            409,
        )
        delivered = update(
            db,
            current,
            now,
            {**current["data"], "delivered_at": now, "delivery_channel": "telegram"},
            "completed",
        )
        audit(
            db,
            "system",
            "consultation_result.delivered",
            delivered,
            now,
            "Telegram подтвердил отправку персонального PDF",
            current["revision"],
        )


def register_consultation_results(app):
    def accessible(id):
        result = object_get(g.db, id, "consultation_result")
        require(
            result["owner"] == g.actor["id"] or g.actor["role"] == "admin",
            "Результат не найден",
            404,
        )
        return result

    @app.get("/api/bookings/<id>/result")
    @protected()
    def booking_result(id):
        booking = object_get(g.db, id, "booking")
        require(
            booking["owner"] == g.actor["id"] or g.actor["role"] == "admin",
            "Встреча не найдена",
            404,
        )
        require(booking["data"].get("result_id"), "Персональный результат ещё не запрошен", 404)
        return accessible(booking["data"]["result_id"])

    @app.get("/api/consultation-results/<id>")
    @protected()
    def consultation_result(id):
        return accessible(id)

    @app.get("/api/consultation-results/<id>/pdf")
    @protected()
    def consultation_result_pdf(id):
        result = accessible(id)
        pdf = published_pdf(g.db, result)
        audit(
            g.db,
            g.actor["id"],
            "consultation_result.download",
            result,
            g.now,
            "Приватное скачивание персонального результата",
        )
        return Response(
            pdf,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="consultation-result.pdf"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.post("/api/admin/consultation-results/<id>/publish")
    @protected(admin=True)
    def publish_consultation_result(id):
        d = body()
        why = reason(d)
        result = object_get(g.db, id, "consultation_result")
        attachment_id = d.get("attachment_id")
        require(isinstance(attachment_id, str) and attachment_id, "Выберите загруженный PDF")
        if result["status"] in ("delivery_pending", "completed"):
            require(
                result["data"]["answer_attachments"] == [attachment_id],
                "Другой результат уже опубликован",
                409,
            )
            return result
        require(
            result["status"] == "in_progress" and result["revision"] == d.get("revision"),
            "Результат изменён, обновите карточку",
            409,
        )
        response_attachments(g.db, result, [attachment_id])
        media = object_get(g.db, attachment_id, "media", result["owner"])
        require(media["data"].get("mime") == "application/pdf", "Выберите персональный PDF")
        published = update(
            g.db,
            result,
            g.now,
            {
                **result["data"],
                "answer_attachments": [attachment_id],
                "published_at": g.now,
                "published_by": g.actor["id"],
            },
            "delivery_pending",
        )
        audit(
            g.db,
            g.actor["id"],
            "consultation_result.publish",
            published,
            g.now,
            why,
            result["revision"],
        )
        emit(g.db, result["owner"], id, "consultation_result.published", g.now)
        return published
