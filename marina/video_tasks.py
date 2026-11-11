"""Human-assigned private video tasks; financial receipts are never invented."""

import copy
import hashlib
import json
import math
import os
import subprocess
import tempfile
from flask import g, request, Response
from . import Problem, body, object_get, protected, reason, require
from .commerce import policies
from .db import audit, emit, insert, uid, update
from .media import verify_video
from .privacy import consent_required
from .screening import idempotent


def payment_confirmed(task):
    """A video assignment is available only after durable payment evidence."""
    return bool(
        (type(task["data"].get("paid_at")) is int and task["data"]["paid_at"] > 0)
        or task["data"].get("manual_payment", {}).get("status") == "confirmed"
    )


def require_response_audio(db, task, attachments):
    """Validate selected audio before its atomic publication with the specialist answer."""
    audio_count = 0
    for id in attachments:
        media = object_get(db, id, "media", task["owner"])
        mime = media["data"].get("mime")
        if mime not in ("audio/mpeg", "audio/ogg"):
            continue
        blob = db.execute(
            "SELECT body,mime FROM blobs WHERE id=? AND owner=? AND case_id=?",
            (id, task["owner"], task["id"]),
        ).fetchone()
        require(blob and blob["mime"] == mime, "Аудиофайл ответа недоступен")
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg" if mime == "audio/ogg" else ".mp3") as f:
                f.write(blob["body"])
                f.flush()
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-show_entries",
                        "stream=codec_type,codec_name:format=duration,format_name",
                        "-of",
                        "json",
                        f.name,
                    ],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            require(probe.returncode == 0, "Аудио не прошло техническую проверку")
            info = json.loads(probe.stdout)
            duration = float(info.get("format", {}).get("duration", 0))
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise Problem("Не удалось проверить аудио. Проверьте ffprobe и файл ответа") from None
        audio_format, codec = ("ogg", "opus") if mime == "audio/ogg" else ("mp3", "mp3")
        require(
            math.isfinite(duration)
            and duration > 0
            and audio_format in info.get("format", {}).get("format_name", "").split(",")
            and any(
                stream.get("codec_type") == "audio" and stream.get("codec_name") == codec
                for stream in info.get("streams", [])
            ),
            "Прикрепите читаемый аудиоразбор в MP3 или OGG/Opus",
        )
        audio_count += 1
    require(audio_count > 0, "Для публикации разбора прикрепите аудиоответ в MP3 или OGG/Opus")


def register_video_tasks(app):
    app.config.setdefault("LAVA_VIDEO_TASK_OFFER_ID", os.getenv("LAVA_VIDEO_TASK_OFFER_ID", ""))

    @app.post("/api/video-tasks")
    @protected()
    def create_task():
        d = body()
        lead = object_get(g.db, d.get("lead_id"), "consultation_lead", g.actor["id"])
        require(
            lead["data"]["lead_type"] == "baby_0_2" and lead["data"]["action"] == "booking",
            "Нужна заявка на видеоразбор",
        )
        route = lead["data"]["route_snapshot"]
        require(
            all(
                type(route.get(k)) is int and route[k] > 0
                for k in ("required_videos", "response_hours")
            ),
            "Специалист должен настроить количество видео и срок ответа",
        )
        require(
            route["required_videos"] <= 6,
            "Проверьте ограничения задания",
        )
        rules = {k: route[k] for k in ("required_videos", "response_hours")}
        if "max_video_seconds" in route:
            # Preserve the fingerprint for retries of leads created before duration was unlimited.
            rules["max_video_seconds"] = route["max_video_seconds"]
        payload = {
            "lead_id": lead["id"],
            "amount_rub": lead["data"]["amount_rub"],
            "rules": rules,
            "assignment": None,
            "videos": [],
            "recommendations": None,
            "due_at": None,
            "order_id": None,
            "paid_at": None,
        }

        def create():
            task = insert(
                g.db, "video_task", payload, g.now, g.actor["id"], status="awaiting_payment"
            )
            audit(
                g.db,
                g.actor["id"],
                "video_task.create",
                task,
                g.now,
                "Запрошено задание и ручной разбор специалистом",
            )
            update(g.db, lead, g.now, {**lead["data"], "video_task_id": task["id"]}, "closed")
            return task

        return idempotent(g.db, "video-task:" + g.actor["id"], lead["id"], payload, create), 201

    @app.post("/api/video-tasks/<id>/quote")
    @protected()
    def quote(id):
        body()  # The client cannot provide a price or an external offer identifier.
        task = object_get(g.db, id, "video_task", g.actor["id"])
        require(not payment_confirmed(task), "Видеоразбор уже оплачен", 409)
        require(task["status"] == "awaiting_payment", "Оплата для задания уже закрыта", 409)
        offer_id = app.config.get("LAVA_VIDEO_TASK_OFFER_ID", "").strip()
        require(offer_id, "Оплата видеоразбора пока не настроена", 503)
        from .commerce import provider_enabled
        from .scope import require_service

        require_service(g.db, "video_task")
        require(provider_enabled(app, "lava"), "Этот способ оплаты отключён")
        operations = policies(g.db, "operations")
        require(
            not operations or not operations["data"].get("pause_sales"),
            "Приём новых заказов приостановлен",
        )
        intent = idempotent(
            g.db,
            "video-task-intent:" + g.actor["id"],
            task["id"],
            {},
            lambda: insert(
                g.db,
                "purchase_intent",
                {"service": "video_task", "subject_ids": [task["id"]], "reference": task["id"]},
                g.now,
                g.actor["id"],
                status="open",
            ),
        )
        for row in g.db.execute(
            "SELECT * FROM objects WHERE kind='quote' AND owner=? "
            "AND json_extract(data,'$.intent_id')=? ORDER BY created DESC,rowid DESC",
            (g.actor["id"], intent["id"]),
        ):
            from .db import unpack

            existing = unpack(row)
            if existing["status"] == "valid" and existing["data"]["expires"] > g.now:
                return existing
        policy = policies(g.db, "commerce", "consultation")
        require(policy, "Опубликуйте правила покупки видеоразбора")
        ttl = policy["data"].get("quote_ttl_seconds")
        require(type(ttl) is int and ttl > 0, "Проверьте срок действия расчёта")
        lead = object_get(g.db, task["data"]["lead_id"], "consultation_lead", g.actor["id"])
        route = lead["data"]["route_snapshot"]
        amount = task["data"]["amount_rub"] * 100
        product = {
            "service": "video_task",
            "title": "Видеоразбор малыша до 3 лет",
            "terms": route["text"],
            "amount": amount,
            "currency": "RUB",
            "lava_offer_id": offer_id,
            "launch_approved": True,
            "web_sales_approved": True,
        }
        data = {
            "intent_id": intent["id"],
            "product_id": "video-task-dynamic",
            "product_snapshot": product,
            "policy_id": policy["id"],
            "policy_snapshot": copy.deepcopy(policy["data"]),
            "video_task_id": task["id"],
            "family": [],
            "lines": [
                {
                    "id": uid(),
                    "subject_id": task["id"],
                    "base": amount,
                    "discount": 0,
                    "amount": amount,
                }
            ],
            "total": amount,
            "currency": "RUB",
            "provider": "lava",
            "surface": "telegram",
            "expires": g.now + ttl,
        }
        result = insert(g.db, "quote", data, g.now, g.actor["id"], status="valid")
        audit(g.db, g.actor["id"], "quote.create", result, g.now, "Цена видеоразбора из заявки")
        return result, 201

    @app.get("/api/video-tasks/<id>")
    @protected()
    def task(id):
        return object_get(g.db, id, "video_task", g.actor["id"])

    @app.post("/api/admin/video-tasks/<id>/confirm-payment")
    @protected(admin=True)
    def confirm_payment(id):
        d = body()
        why = reason(d)
        task = object_get(g.db, id, "video_task")
        reference = d.get("reference")
        require(
            isinstance(reference, str) and 1 <= len(reference.strip()) <= 240,
            "Укажите основание подтверждения оплаты (1–240 символов)",
        )
        reference = reference.strip()
        amount = d.get("amount_rub")
        require(type(amount) is int and amount > 0, "Укажите целую положительную сумму в рублях")
        revision = d.get("revision")
        require(type(revision) is int, "Задание изменено, обновите карточку", 409)
        existing = task["data"].get("manual_payment")
        if existing and existing.get("status") == "confirmed":
            require(
                existing["amount_rub"] == amount
                and existing["reference"] == reference
                and existing["reason"] == why
                and existing["confirmed_by"] == g.actor["id"],
                "Подтверждение оплаты уже записано и не может быть заменено",
                409,
            )
            require(
                revision in (existing["request_revision"], task["revision"]),
                "Задание изменено, обновите карточку",
                409,
            )
            return task
        require(
            task["status"] in ("awaiting_payment", "requested"),
            "Оплату нельзя подтвердить для закрытого или уже выполняемого разбора",
            409,
        )
        require(task["revision"] == revision, "Задание изменено, обновите карточку", 409)
        require(
            type(task["data"].get("amount_rub")) is int and amount == task["data"]["amount_rub"],
            "Сумма должна совпадать со стоимостью в заявке",
        )
        payment = {
            "status": "confirmed",
            "amount_rub": amount,
            "confirmed_at": g.now,
            "confirmed_by": g.actor["id"],
            "reference": reference,
            "reason": why,
            "request_revision": revision,
        }
        result = update(
            g.db,
            task,
            g.now,
            {**task["data"], "manual_payment": payment, "paid_at": g.now},
            "requested",
        )
        audit(
            g.db,
            g.actor["id"],
            "video_task.confirm-payment",
            result,
            g.now,
            why,
            task["revision"],
        )
        emit(g.db, result["owner"], id, "video_task.paid", g.now)
        return result

    @app.post("/api/admin/video-tasks/<id>/<action>")
    @protected(admin=True)
    def specialist_action(id, action):
        d = body()
        why = reason(d)
        task = object_get(g.db, id, "video_task")
        require(task["revision"] == d.get("revision"), "Задание изменено, обновите карточку", 409)
        text = d.get("text", "")
        if action in ("assignment", "recommendations"):
            require(
                isinstance(text, str) and 0 < len(text.strip()) <= 16000,
                "Напишите текст специалиста",
            )
        if action == "close":
            require(
                task["status"] in ("awaiting_payment", "requested", "awaiting_videos", "submitted"),
                "Разбор уже завершён",
                409,
            )
            payload = {
                **task["data"],
                "closed_at": g.now,
                "closed_by": g.actor["id"],
                "due_at": None,
            }
            state = "closed"
        elif action == "assignment":
            require(payment_confirmed(task), "Сначала подтвердите оплату видеоразбора", 409)
            require(task["status"] == "requested", "Задание уже выдано", 409)
            payload = {
                **task["data"],
                "assignment": text.strip(),
                "assigned_by": g.actor["id"],
                "assigned_at": g.now,
            }
            state = "awaiting_videos"
        elif action == "recommendations":
            require(task["status"] == "submitted", "Дождитесь всех видео и отправки на разбор")
            from .attachments import response_attachments

            attachments = response_attachments(g.db, task, d.get("attachments", []))
            require_response_audio(g.db, task, attachments)
            payload = {
                **task["data"],
                "answer_attachments": attachments,
                "recommendations": text.strip(),
                "answered_by": g.actor["id"],
                "answered_at": g.now,
            }
            state = "completed"
        else:
            require(False, "Неизвестное действие", 404)
        result = update(g.db, task, g.now, payload, state)
        audit(g.db, g.actor["id"], "video_task." + action, result, g.now, why, task["revision"])
        emit(g.db, result["owner"], id, "video_task." + action, g.now)
        return result

    @app.post("/api/video-tasks/<id>/video")
    @protected()
    def upload(id):
        consent_required(g.db, g.actor["id"])
        task = object_get(g.db, id, "video_task", g.actor["id"])
        require(payment_confirmed(task), "Сначала оплатите видеоразбор", 409)
        require(
            task["status"] == "awaiting_videos" and task["data"].get("assignment"),
            "Дождитесь задания специалиста",
            409,
        )
        p = policies(g.db, "privacy")
        limit = min(p["data"].get("media_max_bytes", 0), app.config["MAX_CONTENT_LENGTH"])
        require(limit > 0 and p["data"].get("retention_days", 0) > 0, "Настройте хранение видео")
        try:
            slot = int(request.form.get("slot", ""))
        except (TypeError, ValueError):
            slot = 0
        require(1 <= slot <= task["data"]["rules"]["required_videos"], "Выберите пункт задания")
        file = request.files.get("file")
        require(file and file.mimetype == "video/mp4", "Отправьте видео MP4")
        raw = file.read(limit + 1)
        require(0 < len(raw) <= limit and raw[4:8] == b"ftyp", "Проверьте размер и формат MP4")
        digest = hashlib.sha256(raw).hexdigest()
        prior = next((v for v in task["data"]["videos"] if v["slot"] == slot), None)
        if prior:
            require(
                prior["sha256"] == digest,
                "Видео для этого пункта уже получено. Для замены свяжитесь со специалистом",
                409,
            )
            return task
        # Duration is unrestricted, including tasks with an old max_video_seconds snapshot.
        verify_video(raw)
        asset = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (asset, task["owner"], id, id, "video/mp4", digest, raw, g.now),
        )
        insert(
            g.db,
            "media",
            {
                "case_id": id,
                "purpose": "video_task",
                "mime": "video/mp4",
                "size": len(raw),
                "retention_policy_id": p["id"],
                "expires": g.now + p["data"]["retention_days"] * 86400,
            },
            g.now,
            task["owner"],
            id,
            status="ready",
            id=asset,
        )
        videos = sorted(
            [*task["data"]["videos"], {"slot": slot, "asset_id": asset, "sha256": digest}],
            key=lambda v: v["slot"],
        )
        result = update(g.db, task, g.now, {**task["data"], "videos": videos})
        audit(
            g.db,
            g.actor["id"],
            "video_task.upload",
            result,
            g.now,
            "Видео выполнения задания",
            task["revision"],
        )
        return result

    @app.get("/api/video-tasks/<id>/video/<int:slot>")
    @protected()
    def download(id, slot):
        task = object_get(g.db, id, "video_task")
        require(task["owner"] == g.actor["id"] or g.actor["role"] == "admin", "Нет доступа", 404)
        video = next((v for v in task["data"]["videos"] if v["slot"] == slot), None)
        require(video, "Видео не найдено", 404)
        blob = g.db.execute(
            "SELECT body FROM blobs WHERE id=? AND case_id=?", (video["asset_id"], id)
        ).fetchone()
        require(blob, "Видео удалено по правилам хранения", 404)
        audit(
            g.db, g.actor["id"], "video_task.view", task, g.now, "Просмотр видео выполнения задания"
        )
        return Response(blob["body"], mimetype="video/mp4")

    @app.post("/api/video-tasks/<id>/submit")
    @protected()
    def submit(id):
        body()
        task = object_get(g.db, id, "video_task", g.actor["id"])
        if task["status"] in ("submitted", "completed"):
            return task
        require(
            payment_confirmed(task)
            and task["status"] == "awaiting_videos"
            and len(task["data"]["videos"]) == task["data"]["rules"]["required_videos"],
            "Выполните задание и загрузите видео по всем пунктам",
        )
        result = update(
            g.db,
            task,
            g.now,
            {
                **task["data"],
                "submitted_at": g.now,
                "due_at": g.now + task["data"]["rules"]["response_hours"] * 3600,
            },
            "submitted",
        )
        emit(g.db, result["owner"], id, "video_task.submitted", g.now)
        audit(
            g.db,
            g.actor["id"],
            "video_task.submit",
            result,
            g.now,
            "Родитель отправил выполнение на ручной разбор",
            task["revision"],
        )
        return result
