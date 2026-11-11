"""Telegram controls for parent videos and specialist recommendations."""

import io
from urllib.request import urlopen
from . import Problem, require
from .providers import telegram


def show_task(bot, id):
    task = bot.api("/video-tasks/" + id)
    d = task["data"]
    buttons = []
    text = "📹 Видеоразбор выполнения задания\n\n"
    text += (
        d.get("recommendations")
        or d.get("assignment")
        or "Специалист подготовит задание. Оно придёт в этот чат. Не нужно заранее снимать случайные видео."
    )
    paid = type(d.get("paid_at")) is int and d["paid_at"] > 0
    payment = d.get("manual_payment", {})
    if paid or payment.get("status") == "confirmed":
        text += "\n\n✅ Оплата подтверждена"
    else:
        from .consultation_journey import display_price

        price = display_price(d["amount_rub"] * 100)
        text += "\n\nСначала оплатите видеоразбор. После подтверждения специалист получит заявку и подготовит задание."
        if task["status"] == "awaiting_payment":
            buttons.append(("💳 Оплатить видеоразбор за " + price, "video_task_pay", {"id": id}))
    if task["status"] == "closed":
        text += "\n\nВидеоразбор закрыт. Для продолжения свяжитесь со специалистом."
    if task["status"] == "awaiting_videos":
        filled = {v["slot"] for v in d["videos"]}
        buttons = [
            (
                "📹 Отправить видео: пункт " + str(slot),
                "video_task_upload",
                {"id": id, "slot": slot},
            )
            for slot in range(1, d["rules"]["required_videos"] + 1)
            if slot not in filled
        ]
        text += (
            "\n\nПолучено видео: "
            + str(len(filled))
            + " из "
            + str(d["rules"]["required_videos"])
            + "."
        )
        if len(filled) == d["rules"]["required_videos"]:
            buttons.append(("Отправить специалисту на разбор", "video_task_submit", {"id": id}))
    elif task["status"] == "submitted":
        text += "\n\nВидео отправлены на разбор. Коррекция и рекомендации придут в этот чат."
    if task["status"] == "completed" and d.get("answer_attachments"):
        buttons.append(("Скачать материалы разбора", "video_task_answer_download", {"id": id}))
    bot.send(text, buttons + [("В меню", "menu", {})])


def handle_action(bot, action, d):
    if action == "video_task_open":
        show_task(bot, d["id"])
    elif action == "video_task_pay":
        task = bot.api("/video-tasks/" + d["id"])
        if type(task["data"].get("paid_at")) is int and task["data"]["paid_at"] > 0:
            show_task(bot, d["id"])
            return True
        quote = bot.api("/video-tasks/" + d["id"] + "/quote", "POST", {})
        order = bot.api(
            "/orders",
            "POST",
            {"quote_id": quote["id"], "key": "bot-video-task:" + quote["id"]},
        )
        if order["status"] == "paid":
            show_task(bot, d["id"])
            return True
        bot.state_set(step="funnel_checkout_email", funnel_order_id=order["id"])
        bot.send(
            "Укажите email для электронного чека Lava.top. "
            "Он используется только для оформления платежа."
        )
    elif action == "video_task_upload":
        task = bot.api("/video-tasks/" + d["id"])
        require(
            task["status"] == "awaiting_videos"
            and type(d.get("slot")) is int
            and 1 <= d["slot"] <= task["data"]["rules"]["required_videos"],
            "Выберите пункт действующего задания",
        )
        bot.state_set(step="video_task_upload", video_task_id=d["id"], video_task_slot=d["slot"])
        bot.send(
            "Пришлите видео выполнения пункта "
            + str(d["slot"])
            + " одним сообщением: MP4. Ограничения по длительности нет. Видео увидит специалист."
        )
    elif action == "video_task_submit":
        bot.api("/video-tasks/" + d["id"] + "/submit", "POST", {})
        bot.state_set(step=None)
        show_task(bot, d["id"])
    elif action == "admin_video_task_files":
        bot.require_admin()
        task = bot.api("/admin/objects/" + d["id"])["object"]
        require(task["kind"] == "video_task", "Выберите видеоразбор")
        from .bot import send_file

        for v in task["data"]["videos"]:
            r = bot.client.get("/api/video-tasks/" + d["id"] + "/video/" + str(v["slot"]))
            require(r.status_code == 200, "Видео недоступно")
            send_file(
                bot.app,
                bot.chat,
                "assignment-" + str(v["slot"]) + ".mp4",
                r.data,
                "video/mp4",
                "sendVideo",
            )
    else:
        return False
    return True


def receive(bot, message):
    attachment = message.get("video") or message.get("document")
    require(attachment, "Отправьте видео или файл MP4")
    privacy = bot.api("/privacy")
    require(privacy["accepted"], "Сначала примите актуальные условия обработки данных")
    limit = min(privacy["policy"]["data"].get("media_max_bytes", 0), 20 * 1024 * 1024)
    require(
        limit > 0 and attachment.get("file_size", 0) <= limit, "Видео превышает допустимый размер"
    )
    info = telegram(bot.app, "getFile", {"file_id": attachment["file_id"]})
    path = info.get("file_path", "")
    require(path and ".." not in path and not path.startswith("/"), "Не удалось прочитать видео")
    try:
        if bot.app.config.get("TELEGRAM_FILE_TRANSPORT"):
            raw = bot.app.config["TELEGRAM_FILE_TRANSPORT"](path, limit)
        else:
            with urlopen(
                "https://api.telegram.org/file/bot" + bot.app.config["BOT_TOKEN"] + "/" + path,
                timeout=20,
            ) as r:
                raw = r.read(limit + 1)
    except Exception:
        raise Problem("Не удалось скачать видео. Попробуйте ещё раз.") from None
    require(0 < len(raw) <= limit, "Видео превышает допустимый размер")
    id = bot.state["data"]["video_task_id"]
    r = bot.client.post(
        "/api/video-tasks/" + id + "/video",
        data={
            "slot": str(bot.state["data"]["video_task_slot"]),
            "file": (io.BytesIO(raw), "task.mp4", "video/mp4"),
        },
    )
    require(r.status_code < 400, r.json.get("error", "Не удалось сохранить видео"))
    bot.state_set(step=None)
    show_task(bot, id)
