"""Human-assigned private video tasks; financial receipts are never invented."""

import hashlib
from flask import g, request, Response
from . import body, object_get, protected, reason, require
from .commerce import policies
from .db import audit, emit, insert, uid, update
from .media import verify_video
from .privacy import consent_required
from .screening import idempotent


def register_video_tasks(app):
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
                for k in ("required_videos", "max_video_seconds", "response_hours")
            ),
            "Специалист должен настроить задание, длительность видео и срок ответа",
        )
        require(
            route["required_videos"] <= 6 and route["max_video_seconds"] <= 600,
            "Проверьте ограничения задания",
        )
        payload = {
            "lead_id": lead["id"],
            "amount_rub": lead["data"]["amount_rub"],
            "rules": {
                k: route[k] for k in ("required_videos", "max_video_seconds", "response_hours")
            },
            "assignment": None,
            "videos": [],
            "recommendations": None,
            "due_at": None,
        }

        def create():
            task = insert(g.db, "video_task", payload, g.now, g.actor["id"], status="requested")
            emit(g.db, task["owner"], task["id"], "video_task.requested", g.now)
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

    @app.get("/api/video-tasks/<id>")
    @protected()
    def task(id):
        return object_get(g.db, id, "video_task", g.actor["id"])

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
                task["status"] in ("requested", "awaiting_videos", "submitted"),
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
        info = verify_video(raw)
        require(
            float(info["format"]["duration"]) <= task["data"]["rules"]["max_video_seconds"],
            "Видео длиннее задания специалиста",
        )
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
            task["status"] == "awaiting_videos"
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
