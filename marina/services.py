"""Specialist services. Publication, deadlines and notification are separate facts."""

import hashlib
import io
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from xml.sax.saxutils import escape

from flask import Response, g, request
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

from . import body, object_get, protected, reason, require
from .db import audit, emit, insert, uid, update
from .screening import idempotent


def text_input(d, key="text"):
    text = d.get(key)
    require(
        isinstance(text, str) and 1 <= len(text.strip()) <= 100000,
        "Заполните текст (до 100000 символов)",
    )
    return text.strip()


def render_pdf(text, limitations=""):
    # Bundle a Cyrillic font and its license; rendering does not depend on the host.
    from pathlib import Path

    if "Marina" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont(
                "Marina",
                str(Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"),
            )
        )
    target = io.BytesIO()
    style = ParagraphStyle("body", fontName="Marina", fontSize=11, leading=16, splitLongWords=True)
    story = []
    text = (
        "Нейроразвитие дома\nПерсональный отчёт\n\n"
        + text
        + "\n\nОграничения\nСервис не ставит диагнозы.\n"
        + limitations
    )
    for line in text.splitlines():
        story.extend([Paragraph(escape(line) or " ", style), Spacer(1, 7)])
    SimpleDocTemplate(target, title="Персональный отчёт", author="Нейроразвитие дома").build(story)
    return target.getvalue()


def register_services(app):
    @app.post("/api/admin/reports/<id>/<action>")
    @protected(admin=True)
    def report_action(id, action):
        obj = object_get(g.db, id, "report")
        d, payload = body(), {**obj["data"]}
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Отчёт изменён", 409)
        if action == "revise":
            revisions = list(payload["revisions"])
            revisions.append(
                {
                    "number": len(revisions) + 1,
                    "text": text_input(d),
                    "actor": g.actor["id"],
                    "at": g.now,
                }
            )
            payload.update(revisions=revisions, approved_revision=None)
            state = "draft"
        elif action == "approve":
            n = d.get("text_revision")
            require(
                type(n) is int and n == len(payload["revisions"]) and n > 0,
                "Утвердите текущую редакцию",
            )
            payload.update(approved_revision=n, approved_by=g.actor["id"], approved_at=g.now)
            state = "approved"
        elif action == "publish":
            n = d.get("text_revision")
            require(
                type(n) is int
                and n > 0
                and payload["approved_revision"] == n
                and len(payload["revisions"]) == n,
                "Эта редакция не утверждена",
            )
            attempt = object_get(g.db, payload["attempt_id"], "attempt", obj["owner"])
            pdf = render_pdf(
                payload["revisions"][n - 1]["text"],
                attempt["data"]["method_snapshot"]["limitations"],
            )
            blob_id = uid()
            g.db.execute(
                "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
                (
                    blob_id,
                    obj["owner"],
                    obj["subject"],
                    obj["id"],
                    "application/pdf",
                    hashlib.sha256(pdf).hexdigest(),
                    pdf,
                    g.now,
                ),
            )
            payload.update(published_revision=n, pdf_id=blob_id, published_at=g.now)
            state = "published"
            emit(g.db, obj["owner"], id, "report.published", g.now)
        else:
            require(False, "Неизвестное действие", 404)
        result = update(g.db, obj, g.now, payload, state)
        audit(g.db, g.actor["id"], "report." + action, result, g.now, why, obj["revision"])
        return result

    @app.get("/api/reports/<id>/pdf")
    @protected()
    def report_pdf(id):
        obj = object_get(g.db, id, "report", g.actor["id"])
        require(obj["data"].get("pdf_id"), "PDF ещё готовится", 404)
        blob = g.db.execute(
            "SELECT body FROM blobs WHERE id=? AND owner=?",
            (obj["data"]["pdf_id"], g.actor["id"]),
        ).fetchone()
        require(blob, "Файл недоступен", 404)
        return Response(
            blob["body"],
            mimetype="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
        )

    @app.post("/api/cases/<id>/media")
    @protected()
    def upload_media(id):
        from .commerce import policies

        obj = object_get(g.db, id, "case", g.actor["id"])
        require(obj["status"] == "draft", "Комплект уже отправлен", 409)
        policy = policies(g.db, "privacy")
        require(
            policy
            and policy["data"].get("media_max_bytes")
            and policy["data"].get("retention_days"),
            "Сначала настройте условия хранения файлов",
        )
        file = request.files.get("file")
        require(file, "Выберите файл")
        data = file.read(min(policy["data"]["media_max_bytes"], 20 * 1024 * 1024) + 1)
        require(
            0 < len(data) <= min(policy["data"]["media_max_bytes"], 20 * 1024 * 1024),
            "Файл превышает лимит",
        )
        mime = file.mimetype
        require(
            (mime == "video/mp4" and data[4:8] == b"ftyp")
            or (mime == "audio/ogg" and data.startswith(b"OggS"))
            or (
                mime == "audio/mpeg"
                and (data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
            ),
            "Разрешены MP4, OGG и MP3 с корректной сигнатурой",
        )
        digest = hashlib.sha256(data).hexdigest()
        row = g.db.execute(
            "SELECT id FROM blobs WHERE case_id=? AND digest=?", (id, digest)
        ).fetchone()
        if row:
            return {"id": row["id"], "mime": mime}
        blob_id = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (blob_id, g.actor["id"], obj["subject"], id, mime, digest, data, g.now),
        )
        audit(
            g.db,
            g.actor["id"],
            "media.upload",
            {"id": blob_id},
            g.now,
            "Материал к разбору",
        )
        insert(
            g.db,
            "media",
            {
                "case_id": id,
                "purpose": "client_input",
                "mime": mime,
                "size": len(data),
                "retention_policy_id": policy["id"],
                "expires": g.now + policy["data"]["retention_days"] * 86400,
            },
            g.now,
            g.actor["id"],
            obj["subject"],
            "ready",
            id=blob_id,
        )
        return {"id": blob_id, "mime": mime}, 201

    @app.get("/api/media/<id>")
    @protected()
    def media(id):
        row = g.db.execute("SELECT * FROM blobs WHERE id=?", (id,)).fetchone()
        require(
            row and (row["owner"] == g.actor["id"] or g.actor["role"] == "admin"),
            "Файл не найден",
            404,
        )
        if g.actor["role"] != "admin":
            record = g.db.execute(
                "SELECT * FROM objects WHERE id=? AND kind='media'", (id,)
            ).fetchone()
            if record:
                from .db import unpack

                media = unpack(record)
                if media["data"].get("purpose") == "specialist_response":
                    case = object_get(g.db, media["data"]["case_id"], "case", g.actor["id"])
                    require(
                        id in case["data"].get("answer_attachments", []),
                        "Файл ответа ещё не опубликован",
                        404,
                    )
        return Response(
            row["body"],
            mimetype=row["mime"],
            headers={"Content-Disposition": 'inline; filename="material"'},
        )

    @app.post("/api/cases")
    @protected()
    def prepare_case():
        d = body()
        subject = object_get(g.db, d.get("subject_id"), "subject", g.actor["id"])
        product = object_get(g.db, d.get("product_id"), "product")
        require(
            subject["status"] == "active"
            and product["status"] == "published"
            and product["data"]["service"] == "case",
            "Выберите опубликованный разбор и действующий профиль",
        )
        payload = {
            "product_id": product["id"],
            "product_snapshot": product["data"],
            "paid_at": None,
            "submitted_at": None,
            "due_at": None,
            "problem": "",
            "answers": {},
            "answer": None,
            "followup": None,
        }
        return idempotent(
            g.db,
            "case:" + g.actor["id"],
            d.get("key"),
            {"subject_id": subject["id"], "product_id": product["id"]},
            lambda: insert(g.db, "case", payload, g.now, g.actor["id"], subject["id"]),
        )

    @app.get("/api/cases/<id>")
    @protected()
    def get_case(id):
        from .views import client_view

        return client_view(object_get(g.db, id, "case", g.actor["id"]))

    @app.post("/api/cases/<id>/submit")
    @protected()
    def submit_case(id):
        obj = object_get(g.db, id, "case", g.actor["id"])
        if obj["data"]["submitted_at"] is not None:
            return obj
        d = body()
        problem = text_input(d, "problem")
        answers = d.get("answers")
        require(isinstance(answers, dict) and answers, "Заполните анкету")
        if obj["data"]["product_snapshot"].get("required_media", True):
            require(
                g.db.execute(
                    "SELECT 1 FROM blobs WHERE case_id=? AND owner=?",
                    (id, g.actor["id"]),
                ).fetchone(),
                "Загрузите обязательные материалы",
            )
        product = obj["data"]["product_snapshot"]
        required = [
            q["id"] for q in product.get("case_questions", []) if q.get("required", True)
        ] or product.get("required_fields", [])
        require(
            all(isinstance(answers.get(k), str) and answers[k].strip() for k in required),
            "Заполните обязательные поля анкеты",
        )
        payload = {
            **obj["data"],
            "problem": problem,
            "answers": answers,
            "submitted_at": g.now,
            "due_at": max(obj["data"]["paid_at"], g.now) + 48 * 3600
            if obj["data"]["paid_at"] is not None
            else None,
        }
        result = update(
            g.db,
            obj,
            g.now,
            payload,
            "submitted" if payload["due_at"] is not None else "awaiting_payment",
        )
        if payload["due_at"] is not None:
            emit(g.db, obj["owner"], id, "case.submitted", g.now)
        audit(
            g.db,
            g.actor["id"],
            "case.submit",
            result,
            g.now,
            "Полный комплект отправлен специалисту",
            obj["revision"],
        )
        return result

    @app.post("/api/admin/cases/<id>/<action>")
    @protected(admin=True)
    def case_action(id, action):
        obj = object_get(g.db, id, "case")
        d = body()
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Разбор изменён", 409)
        payload = {**obj["data"]}
        if action == "answer":
            require(
                obj["status"] in ("submitted", "in_progress"),
                "Разбор ещё не готов к ответу",
                409,
            )
            from .attachments import response_attachments

            payload["answer_attachments"] = response_attachments(
                g.db, obj, d.get("attachments", [])
            )
            payload.update(
                answer=text_input(d),
                answered_at=g.now,
                followup_until=g.now + 24 * 3600,
            )
            state = "answered"
        elif action == "correct-answer":
            require(
                obj["status"] in ("answered", "followup_pending", "closed"),
                "Основной ответ ещё не опубликован",
            )
            payload["answer"] = text_input(d)
            payload["corrected_at"] = g.now
            state = obj["status"]
        elif action == "followup-answer":
            require(obj["status"] == "followup_pending", "Нет принятого уточнения", 409)
            payload["followup"] = {
                **payload["followup"],
                "answer": text_input(d),
                "answered_at": g.now,
            }
            state = "closed"
        elif action in ("in_progress", "request-info"):
            require(
                obj["status"] in ("submitted", "in_progress", "followup_pending"),
                "Недопустимое состояние",
                409,
            )
            payload["notes"] = [
                *payload.get("notes", []),
                {"text": text_input(d), "at": g.now, "actor": g.actor["id"]},
            ]
            state = "in_progress" if obj["status"] != "followup_pending" else obj["status"]
        else:
            require(False, "Неизвестное действие", 404)
        result = update(g.db, obj, g.now, payload, state)
        audit(g.db, g.actor["id"], "case." + action, result, g.now, why, obj["revision"])
        emit(g.db, obj["owner"], id, "case." + action, g.now)
        return result

    @app.post("/api/cases/<id>/followup")
    @protected()
    def followup(id):
        obj = object_get(g.db, id, "case", g.actor["id"])
        d = body()
        require(obj["data"]["followup"] is None, "Уточнение уже использовано", 409)
        require(
            obj["status"] == "answered" and g.now < obj["data"]["followup_until"],
            "Окно уточнения завершено",
            422,
        )
        require(d.get("same_problem") is True, "Для новой проблемы нужен отдельный разбор")
        payload = {
            **obj["data"],
            "followup": {
                "text": text_input(d),
                "accepted_at": g.now,
                "due_at": g.now + 48 * 3600,
            },
        }
        result = update(g.db, obj, g.now, payload, "followup_pending")
        emit(g.db, obj["owner"], id, "case.followup", g.now)
        return result

    @app.post("/api/bookings")
    @protected()
    def create_booking():
        d = body()
        s = object_get(g.db, d.get("subject_id"), "subject", g.actor["id"])
        require(s["status"] == "active", "Профиль архивирован")
        payload = {
            "topic": text_input(d, "topic"),
            "preferences": d.get("preferences", ""),
            "proposals": [],
            "current_proposal": None,
        }

        def create():
            obj = insert(g.db, "booking", payload, g.now, g.actor["id"], s["id"], "requested")
            emit(g.db, obj["owner"], obj["id"], "booking.requested", g.now)
            return obj

        return idempotent(
            g.db,
            "booking:" + g.actor["id"],
            d.get("key"),
            {**payload, "subject": s["id"]},
            create,
        )

    @app.post("/api/admin/bookings/<id>/<action>")
    @protected(admin=True)
    def booking_action(id, action):
        obj = object_get(g.db, id, "booking")
        d = body()
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Заявка изменена", 409)
        payload = {**obj["data"]}
        if action == "propose":
            require(obj["status"] not in ("cancelled", "completed"), "Заявка закрыта", 409)
            require(
                type(d.get("starts")) is int
                and d["starts"] > g.now
                and type(d.get("duration_minutes")) is int
                and 5 <= d["duration_minutes"] <= 480,
                "Укажите будущее время и длительность",
            )
            try:
                ZoneInfo(d.get("timezone", ""))
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                require(False, "Укажите часовой пояс IANA")
            prop = {
                "id": uid(),
                "starts": d["starts"],
                "duration_minutes": d["duration_minutes"],
                "timezone": d["timezone"],
                "at": g.now,
            }
            payload.update(
                proposals=[*payload["proposals"], prop],
                current_proposal=prop["id"],
                connection=None,
                confirmed_at=None,
            )
            state = "proposed"
        elif action == "connection":
            require(obj["status"] == "confirmed", "Время не подтверждено")
            link = text_input(d)
            require(
                link.startswith("https://") and len(link) < 2000,
                "Укажите HTTPS-ссылку встречи",
            )
            payload["connection"] = link
            state = obj["status"]
        elif action in ("cancel", "complete"):
            require(
                obj["status"] not in ("cancelled", "completed"),
                "Заявка уже закрыта",
                409,
            )
            if action == "complete":
                require(obj["status"] == "confirmed", "Встреча не подтверждена")
            state = "cancelled" if action == "cancel" else "completed"
        else:
            require(False, "Неизвестное действие", 404)
        result = update(g.db, obj, g.now, payload, state)
        audit(
            g.db,
            g.actor["id"],
            "booking." + action,
            result,
            g.now,
            why,
            obj["revision"],
        )
        emit(g.db, obj["owner"], id, "booking." + action, g.now)
        from .crm import sync_booking

        sync_booking(g.db, result, g.now, g.actor["id"], why)
        return result

    @app.get("/api/bookings/<id>")
    @protected()
    def get_booking(id):
        return object_get(g.db, id, "booking", g.actor["id"])

    @app.post("/api/bookings/<id>/<action>")
    @protected()
    def answer_proposal(id, action):
        obj = object_get(g.db, id, "booking", g.actor["id"])
        d = body()
        require(action in ("confirm", "reschedule", "cancel"), "Неизвестное действие", 404)
        if action in ("reschedule", "cancel"):
            require(obj["status"] not in ("cancelled", "completed"), "Встреча уже закрыта", 409)
        if action == "confirm":
            require(
                obj["data"]["current_proposal"] == d.get("proposal_id"),
                "Предложение заменено, откройте новое время",
                409,
            )
            if obj["status"] == "confirmed":
                return obj
            require(obj["status"] == "proposed", "Нет действующего предложения", 409)
            current = next(p for p in obj["data"]["proposals"] if p["id"] == d["proposal_id"])
            require(current["starts"] > g.now, "Предложенное время уже прошло")
        result = update(
            g.db,
            obj,
            g.now,
            {**obj["data"], "confirmed_at": g.now if action == "confirm" else None},
            {
                "confirm": "confirmed",
                "reschedule": "reschedule_requested",
                "cancel": "cancelled",
            }[action],
        )
        audit(
            g.db,
            g.actor["id"],
            "booking." + action,
            result,
            g.now,
            "Решение клиента о времени",
            obj["revision"],
        )
        emit(g.db, obj["owner"], id, "booking." + action, g.now)
        from .crm import sync_booking

        sync_booking(g.db, result, g.now, g.actor["id"], "Решение клиента о времени")
        return result
