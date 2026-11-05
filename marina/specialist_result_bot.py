"""Explicit specialist publication and authenticated retrieval in private Telegram chats."""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from . import require
from .db import uid


RESULT_STATES = {
    "in_progress": "Специалист готовит персональный документ",
    "delivery_pending": "PDF опубликован, отправка в Telegram ожидается",
    "completed": "Персональный PDF отправлен в Telegram",
}


def when(value):
    return datetime.fromtimestamp(value, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")


def result_summary(result):
    data = result["data"]
    text = data["title"] + "\n\n" + RESULT_STATES.get(result["status"], result["status"])
    if data.get("due_at") is not None:
        text += "\nСрок: " + when(data["due_at"])
    else:
        text += "\nТочный срок выдачи уточняется у специалиста."
    if data.get("delivered_at") is not None:
        text += "\nОтправлено: " + when(data["delivered_at"])
    return text


def show_result(bot, id):
    result = bot.api("/consultation-results/" + id)
    buttons = []
    if result["data"].get("published_at") is not None:
        buttons.append(("Скачать персональный PDF", "consultation_result_download", {"id": id}))
    bot.send(result_summary(result), buttons + [("Мои обращения", "history", {})])


def admin_result_card(bot, result):
    bot.require_admin()
    id = result["id"]
    buttons = []
    if result["status"] == "in_progress":
        buttons.append(("Загрузить персональный PDF", "admin_specialist_result_start", {"id": id}))
    if result["data"].get("published_at") is not None:
        buttons.append(("Скачать персональный PDF", "consultation_result_download", {"id": id}))
    bot.send(
        result_summary(result),
        buttons
        + [
            ("Открыть встречу", "admin_card", {"id": result["data"]["booking_id"]}),
            ("История изменений", "admin_object_versions", {"id": id}),
            ("Аудит объекта", "admin_object_audit", {"id": id}),
            ("В очередь", "queue", {}),
        ],
    )


def _current(bot, pending):
    current = bot.api("/admin/objects/" + pending["id"])["object"]
    require(
        current["kind"] == pending["kind"]
        and current["revision"] == pending["revision"]
        and current["status"]
        == ("in_progress" if current["kind"] == "consultation_result" else "submitted"),
        "Карточка изменена. Откройте её заново и повторите подготовку результата.",
        409,
    )
    return current


def handle_action(bot, action, data):
    if action == "admin_text" and data.get("command") == "video-tasks/recommendations":
        # Previously issued buttons must also lead to the required audio publication flow.
        return handle_action(bot, "admin_specialist_audio_start", data)
    if action == "consultation_result_open":
        show_result(bot, data["id"])
    elif action == "consultation_result_download":
        from .bot import send_file

        result = bot.api("/consultation-results/" + data["id"])
        response = bot.client.get("/api/consultation-results/" + data["id"] + "/pdf")
        require(
            response.status_code == 200, (response.json or {}).get("error", "PDF пока недоступен")
        )
        send_file(bot.app, bot.chat, result["data"]["filename"], response.data, "application/pdf")
    elif action == "video_task_answer_download":
        from .bot import send_file

        task = bot.api("/video-tasks/" + data["id"])
        ids = task["data"].get("answer_attachments", [])
        require(task["status"] == "completed" and ids, "Материалы разбора ещё не опубликованы")
        for asset_id in ids:
            response = bot.client.get("/api/media/" + asset_id)
            require(
                response.status_code == 200,
                (response.json or {}).get("error", "Материал недоступен по правилам хранения"),
            )
            mime = response.mimetype
            formats = {
                "audio/ogg": ("ogg", "sendVoice"),
                "audio/mpeg": ("mp3", "sendAudio"),
                "video/mp4": ("mp4", "sendVideo"),
                "application/pdf": ("pdf", "sendDocument"),
            }
            require(mime in formats, "Формат материала не поддерживается")
            extension, method = formats[mime]
            send_file(
                bot.app,
                bot.chat,
                "recommendations." + extension,
                response.data,
                mime,
                method,
            )
    elif action == "admin_video_payment_start":
        bot.require_admin()
        bot.cancel_admin_prompt()
        task = bot.api("/admin/objects/" + data["id"])["object"]
        require(task["kind"] == "video_task", "Выберите видеоразбор")
        require(
            task["data"].get("manual_payment", {}).get("status") != "confirmed",
            "Оплата уже подтверждена специалистом",
        )
        pending = {
            "type": "manual_video_payment",
            "id": task["id"],
            "revision": task["revision"],
            "amount_rub": task["data"]["amount_rub"],
        }
        bot.state_set(step="admin_manual_payment_reference", admin_pending=pending)
        bot.send(
            "Подтверждается фактически полученная оплата "
            + str(pending["amount_rub"])
            + " ₽ по этой заявке. Укажите основание: идентификатор поступления или другую проверяемую ссылку на платёж.",
            [("Отменить", "admin_card", {"id": task["id"]})],
        )
    elif action in ("admin_specialist_result_start", "admin_specialist_audio_start"):
        bot.require_admin()
        bot.cancel_admin_prompt()
        obj = bot.api("/admin/objects/" + data["id"])["object"]
        kind = "consultation_result" if action == "admin_specialist_result_start" else "video_task"
        require(
            obj["kind"] == kind
            and obj["status"] == ("in_progress" if kind == "consultation_result" else "submitted"),
            "Карточка пока не готова к публикации результата",
        )
        pending = {
            "type": "specialist_result",
            "draft_id": uid(),
            "id": obj["id"],
            "kind": kind,
            "revision": obj["revision"],
        }
        bot.state_set(
            step="admin_specialist_reason"
            if kind == "consultation_result"
            else "admin_specialist_text",
            admin_pending=pending,
        )
        bot.send(
            "Укажите причину загрузки и публикации персонального PDF для журнала аудита."
            if kind == "consultation_result"
            else "Напишите сопровождение аудиоразбора: коррекцию выполнения и рекомендации специалиста.",
            [("Отменить подготовку", "admin_specialist_cancel", {"id": obj["id"]})],
        )
    elif action == "admin_specialist_cancel":
        bot.require_admin()
        bot.cancel_admin_prompt()
        bot.admin_card(data["id"])
    elif action == "admin_specialist_publish":
        bot.require_admin()
        pending = bot.state["data"].get("admin_pending") or {}
        require(
            bot.state["data"].get("step") == "admin_specialist_confirm"
            and pending.get("type") == "specialist_result"
            and all(
                pending.get(key) == data.get(key) for key in ("id", "draft_id", "attachment_id")
            ),
            "Подготовка отменена или изменена. Откройте карточку и подготовьте результат заново.",
            409,
        )
        _current(bot, pending)
        payload = {"revision": pending["revision"], "reason": pending["reason"]}
        if pending["kind"] == "consultation_result":
            path = "/admin/consultation-results/" + pending["id"] + "/publish"
            payload["attachment_id"] = pending["attachment_id"]
        else:
            path = "/admin/video-tasks/" + pending["id"] + "/recommendations"
            payload.update(text=pending["text"], attachments=[pending["attachment_id"]])
        bot.api(path, "POST", payload)
        bot.cancel_admin_prompt()
        bot.send("Результат опубликован. Отправка родителю поставлена в очередь.")
        bot.admin_card(pending["id"])
    else:
        return False
    return True


def handle_message(bot, message):
    step = bot.state["data"].get("step")
    if step in ("admin_manual_payment_reference", "admin_manual_payment_reason"):
        bot.require_admin()
        pending = bot.state["data"].get("admin_pending") or {}
        require(
            pending.get("type") == "manual_video_payment", "Откройте карточку видеоразбора заново"
        )
        text = message.get("text", "").strip()
        if step == "admin_manual_payment_reference":
            require(
                1 <= len(text) <= 240, "Укажите основание подтверждения оплаты (1–240 символов)"
            )
            bot.state_set(
                step="admin_manual_payment_reason", admin_pending={**pending, "reference": text}
            )
            bot.send(
                "Укажите причину для журнала аудита. После этого оплата будет отмечена как подтверждённая специалистом."
            )
        else:
            require(3 <= len(text) <= 1000, "Причина должна содержать от 3 до 1000 символов")
            bot.api(
                "/admin/video-tasks/" + pending["id"] + "/confirm-payment",
                "POST",
                {
                    "revision": pending["revision"],
                    "amount_rub": pending["amount_rub"],
                    "reference": pending["reference"],
                    "reason": text,
                },
            )
            bot.cancel_admin_prompt()
            bot.send("Оплата подтверждена специалистом. Основание сохранено в журнале.")
            bot.admin_card(pending["id"])
        return True
    if step not in (
        "admin_specialist_text",
        "admin_specialist_reason",
        "admin_specialist_file",
        "admin_specialist_confirm",
    ):
        return False
    bot.require_admin()
    pending = bot.state["data"].get("admin_pending") or {}
    require(pending.get("type") == "specialist_result", "Откройте карточку результата заново")
    _current(bot, pending)
    text = message.get("text", "").strip()
    if step == "admin_specialist_text":
        require(0 < len(text) <= 16000, "Напишите текст сопровождения аудиоразбора")
        bot.state_set(step="admin_specialist_reason", admin_pending={**pending, "text": text})
        bot.send("Укажите причину публикации для журнала аудита.")
    elif step == "admin_specialist_reason":
        require(3 <= len(text) <= 1000, "Причина должна содержать от 3 до 1000 символов")
        bot.state_set(step="admin_specialist_file", admin_pending={**pending, "reason": text})
        bot.send(
            "Отправьте персональный PDF документом. До явной публикации родитель его не увидит."
            if pending["kind"] == "consultation_result"
            else "Отправьте аудиоразбор MP3 или OGG/Opus: аудиофайлом, голосовым сообщением или документом. До явной публикации родитель его не увидит."
        )
    elif step == "admin_specialist_file":
        privacy = bot.api("/privacy")
        policy = privacy.get("policy")
        limit = min(
            (policy or {}).get("data", {}).get("media_max_bytes", 0),
            bot.app.config["MAX_CONTENT_LENGTH"],
            20 * 1024 * 1024,
        )
        require(limit > 0, "Настройте политику хранения файлов")
        normalized = message
        if pending["kind"] == "video_task":
            attachment = message.get("voice") or message.get("audio") or message.get("document")
            require(attachment, "Отправьте аудио MP3 или OGG/Opus")
            attachment = dict(attachment)
            if message.get("voice"):
                attachment.setdefault("mime_type", "audio/ogg")
                attachment.setdefault("file_name", "recommendations.ogg")
            require(
                attachment.get("mime_type") in ("audio/mpeg", "audio/ogg"),
                "Нужен аудиоразбор MP3 или OGG/Opus",
            )
            normalized = {"document": attachment}
        content, mime, filename = bot.admin_document(normalized, limit)
        require(
            pending["kind"] != "consultation_result" or mime == "application/pdf",
            "Нужен персональный PDF",
        )
        route = (
            "consultation-results" if pending["kind"] == "consultation_result" else "video-tasks"
        )
        response = bot.client.post(
            "/api/admin/" + route + "/" + pending["id"] + "/media",
            data={"file": (io.BytesIO(content), filename, mime), "reason": pending["reason"]},
        )
        require(
            response.status_code < 400,
            (response.json or {}).get("error", "Не удалось сохранить файл"),
        )
        pending = {**pending, "attachment_id": response.json["id"]}
        bot.state_set(step="admin_specialist_confirm", admin_pending=pending)
        title = (
            "Опубликовать персональный PDF"
            if pending["kind"] == "consultation_result"
            else "Опубликовать аудиоразбор"
        )
        bot.send(
            "Файл загружен приватно. Проверьте материал и подтвердите публикацию родителю."
            + ("\n\nСопровождение:\n" + pending["text"] if pending.get("text") else ""),
            [
                (
                    title,
                    "admin_specialist_publish",
                    {key: pending[key] for key in ("id", "draft_id", "attachment_id")},
                ),
                ("Отменить подготовку", "admin_specialist_cancel", {"id": pending["id"]}),
            ],
        )
    else:
        bot.send("Для отправки родителю нажмите кнопку публикации или отмените подготовку.")
    return True
