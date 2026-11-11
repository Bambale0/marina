"""Per-recipient delivery checkpoints; no transaction spans external Telegram."""

from . import object_get, require
from .db import connect, insert, transaction
from .providers import telegram


def send_once(app, key, send):
    with connect(app.config["DATABASE"]) as db:
        if db.execute("SELECT 1 FROM runtime WHERE key=?", (key,)).fetchone():
            return
    send()
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "INSERT OR IGNORE INTO runtime VALUES(?,?)", (key, str(int(app.config["CLOCK"]())))
        )


def notify_specialist_event(app, owner, id, event):
    from .bot import Bot, send_file

    with connect(app.config["DATABASE"]) as db:
        obj = object_get(db, id, owner=owner)
        account = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
        require(account, "Получатель не найден")
    if event in ("video_task.assignment", "video_task.recommendations", "video_task.close"):
        if account["blocked"]:
            return
        expected = {
            "video_task.assignment": "awaiting_videos",
            "video_task.recommendations": "completed",
            "video_task.close": "closed",
        }
        if obj["status"] != expected[event]:
            return
        bot = Bot(
            app,
            {"id": account["telegram_id"], "first_name": account["name"]},
            account["telegram_id"],
            "video-task:" + id + ":" + event,
        )
        if event.endswith("assignment"):
            text = (
                "📹 Задание специалиста\n\n"
                + obj["data"]["assignment"]
                + "\n\nСнимите выполнение ребёнком: "
                + str(obj["data"]["rules"]["required_videos"])
                + " видео. Ограничения по длительности нет. Оплата уже подтверждена."
            )
        elif event == "video_task.close":
            text = "Видеоразбор закрыт специалистом. Если хотите продолжить, свяжитесь со специалистом лично."
        else:
            text = (
                "✅ Разбор выполнения и рекомендации специалиста\n\n"
                + obj["data"]["recommendations"]
            )
        send_once(
            app,
            event + ":" + id + ":client",
            lambda: bot.send(text, [("Открыть видеоразбор", "video_task_open", {"id": id})]),
        )
        if event == "video_task.recommendations":
            with connect(app.config["DATABASE"]) as db:
                attachments = [
                    (
                        aid,
                        db.execute(
                            "SELECT body,mime FROM blobs WHERE id=? AND case_id=?", (aid, id)
                        ).fetchone(),
                    )
                    for aid in obj["data"].get("answer_attachments", [])
                ]
            for aid, blob in attachments:
                require(blob, "Материал рекомендаций недоступен")
                method = {
                    "audio/mpeg": "sendAudio",
                    "audio/ogg": "sendVoice",
                    "video/mp4": "sendVideo",
                }.get(blob["mime"], "sendDocument")
                extension = {"audio/mpeg": "mp3", "audio/ogg": "ogg", "video/mp4": "mp4"}.get(
                    blob["mime"], "pdf"
                )
                send_once(
                    app,
                    event + ":" + id + ":client:attachment:" + aid,
                    lambda blob=blob, method=method, extension=extension: send_file(
                        app,
                        account["telegram_id"],
                        "recommendations." + extension,
                        blob["body"],
                        blob["mime"],
                        method,
                    ),
                )
        return
    require(app.config["ADMIN_TELEGRAM_IDS"], "Укажите специалиста для уведомлений")
    for chat in sorted(app.config["ADMIN_TELEGRAM_IDS"]):
        key = event + ":" + id + ":admin:" + str(chat)
        with connect(app.config["DATABASE"]) as db:
            specialist = db.execute(
                "SELECT * FROM accounts WHERE telegram_id=?", (chat,)
            ).fetchone()
            if specialist and (specialist["blocked"] or specialist["role"] != "admin"):
                continue
            acknowledged = db.execute("SELECT 1 FROM runtime WHERE key=?", (key,)).fetchone()
        if not acknowledged:
            if obj["kind"] == "consultation_lead":
                text = (
                    "⚡️ НЕСТАНДАРТНЫЙ ЗАПРОС В БОТЕ!\nКатегория: "
                    + obj["data"]["label"]
                    + "\nКлиент: "
                    + account["name"]
                    + "\nДействие: "
                    + {
                        "booking": "Запрос записи",
                        "contact": "Вопрос специалисту",
                        "download": "Запрошен чек-лист",
                    }[obj["data"]["action"]]
                    + "\nСтоимость услуги: "
                    + str(obj["data"]["amount_rub"])
                    + " ₽"
                )
            else:
                text = (
                    "📹 Видеоразбор: "
                    + (
                        "видео получены, нужен разбор и рекомендации"
                        if event.endswith("submitted")
                        else "оплата подтверждена, родитель ждёт задания специалиста"
                        if event.endswith("paid")
                        else "родитель ждёт задания специалиста"
                    )
                    + "\nКлиент: "
                    + account["name"]
                    + "\nОбращение: "
                    + id
                )
            payload = {
                "chat_id": chat,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Написать родителю",
                                "url": "tg://user?id=" + str(account["telegram_id"]),
                            }
                        ]
                    ]
                },
            }
            if specialist:
                with transaction(app.config["DATABASE"]) as db:
                    callback = insert(
                        db,
                        "bot_callback",
                        {"action": "admin_card", "id": id},
                        int(app.config["CLOCK"]()),
                        specialist["id"],
                        status="active",
                    )
                payload["reply_markup"]["inline_keyboard"].append(
                    [{"text": "Открыть обращение", "callback_data": "b:" + callback["id"]}]
                )
            send_once(app, key, lambda: telegram(app, "sendMessage", payload))
        if event == "video_task.submitted":
            with connect(app.config["DATABASE"]) as db:
                blobs = [
                    (
                        v["slot"],
                        db.execute(
                            "SELECT body,mime FROM blobs WHERE id=? AND case_id=?",
                            (v["asset_id"], id),
                        ).fetchone(),
                    )
                    for v in obj["data"]["videos"]
                ]
            for slot, blob in blobs:
                require(blob, "Видео не найдено")
                send_once(
                    app,
                    key + ":video:" + str(slot),
                    lambda slot=slot, blob=blob: send_file(
                        app,
                        chat,
                        "assignment-" + str(slot) + ".mp4",
                        blob["body"],
                        blob["mime"],
                        "sendVideo",
                    ),
                )
