"""Specialist response attachments are stored privately before publication."""

import hashlib
from flask import g, request
from . import object_get, protected, reason, require
from .db import audit, insert, uid


def response_attachments(db, case, ids):
    require(
        isinstance(ids, list) and all(isinstance(id, str) for id in ids), "Неверные вложения ответа"
    )
    for id in ids:
        media = object_get(db, id, "media", case["owner"])
        require(
            media["data"].get("case_id") == case["id"]
            and media["data"].get("purpose") == "specialist_response"
            and media["status"] == "ready",
            "Вложение ответа ещё не готово или относится к другому разбору",
        )
        require(
            db.execute("SELECT 1 FROM blobs WHERE id=?", (id,)).fetchone(),
            "Файл ответа отсутствует",
        )
    return ids


def register_attachments(app):
    @app.post("/api/admin/cases/<id>/media")
    @app.post("/api/admin/video-tasks/<id>/media")
    @app.post("/api/admin/consultation-results/<id>/media")
    @protected(admin=True)
    def upload_response(id):
        parent_kind = (
            "consultation_result"
            if request.path.startswith("/api/admin/consultation-results/")
            else "video_task"
            if request.path.startswith("/api/admin/video-tasks/")
            else "case"
        )
        obj = object_get(g.db, id, parent_kind)
        require(
            obj["status"] in ("submitted", "in_progress", "followup_pending"),
            "Сначала должен быть отправлен полный комплект",
        )
        why = reason({"reason": request.form.get("reason", "")})
        file = request.files.get("file")
        require(file, "Выберите файл")
        from .commerce import policies

        policy = policies(g.db, "privacy")
        limit = 20 * 1024 * 1024
        if parent_kind == "consultation_result":
            require(policy, "Настройте политику хранения персональных результатов")
            limit = min(
                limit, policy["data"].get("media_max_bytes", 0), app.config["MAX_CONTENT_LENGTH"]
            )
            require(
                limit > 0 and policy["data"].get("retention_days", 0) > 0,
                "Настройте размер и срок хранения PDF",
            )
            require(
                file.mimetype == "application/pdf", "Персональный письменный результат нужен в PDF"
            )
        data = file.read(limit + 1)
        require(0 < len(data) <= limit, "Файл превышает разрешённый размер")
        mime = file.mimetype
        valid = (
            (mime == "video/mp4" and data[4:8] == b"ftyp")
            or (mime == "audio/ogg" and data.startswith(b"OggS"))
            or (
                mime == "audio/mpeg"
                and (data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
            )
            or (mime == "application/pdf" and data.startswith(b"%PDF-"))
        )
        require(valid, "Разрешены MP4, OGG, MP3 и PDF с корректной сигнатурой")
        if mime == "video/mp4":
            from .media import verify_video

            verify_video(data)
        blob_id = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                blob_id,
                obj["owner"],
                obj["subject"] or obj["id"],
                id,
                mime,
                hashlib.sha256(data).hexdigest(),
                data,
                g.now,
            ),
        )
        require(policy or app.config.get("TESTING"), "Настройте политику хранения")
        expires = g.now + (policy["data"]["retention_days"] if policy else 1) * 86400
        media = insert(
            g.db,
            "media",
            {
                "case_id": id,
                "purpose": "specialist_response",
                "mime": mime,
                "size": len(data),
                "expires": expires,
                "retention_policy_id": policy["id"] if policy else None,
            },
            g.now,
            obj["owner"],
            obj["subject"] or obj["id"],
            "ready",
            id=blob_id,
        )
        audit(g.db, g.actor["id"], parent_kind + ".response-upload", media, g.now, why)
        return media, 201
