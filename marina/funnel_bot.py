"""Telegram presentation for the public screening funnel API."""

from . import require
from .db import connect
from .funnel import AGES, published, render_copy


def age_buttons(entry_id):
    return [
        (label, "funnel_age", {"entry_id": entry_id, "age_group": group})
        for group, label in AGES.items()
    ] + [("Другой возраст", "funnel_age", {"entry_id": entry_id, "age_group": "outside"})]


def show_question(bot, run):
    if run["status"] == "completed":
        bot.send("Обрабатываю ваши ответы и готовлю результат… ⏳")
        return
    require(run["status"] == "in_progress", "Этот тест закрыт. Начните заново: /start")
    q = run["data"]["question"]
    letters = "АБВ"
    bot.send(
        q["text"]
        + "\n\n"
        + "\n\n".join(f"{letters[i]}. {o['text']}" for i, o in enumerate(q["options"])),
        [
            (
                letters[i],
                "funnel_answer",
                {
                    "id": run["id"],
                    "revision": run["revision"],
                    "question": q["id"],
                    "value": o["id"],
                },
            )
            for i, o in enumerate(q["options"])
        ],
    )


def handle_action(bot, action, d):
    if action == "welcome":
        bot.state_set(
            step=None, onboarding=False, age_group=None, concern=None, onboarding_next=None
        )
        entry = bot.api("/funnel-entries", "POST", {"key": "bot:" + bot.update_id})
        bot.state_set(funnel_entry_id=entry["id"])
        text = bot.api("/funnel/welcome")["text"]
        buttons = age_buttons(entry["id"])
        if bot.state["data"].get("funnel_run_id"):
            buttons.append(("Продолжить / открыть результат", "funnel_resume", {}))
        bot.send(render_copy(text, {}, bot.user.get("first_name", "")), buttons)
    elif action == "funnel_resume":
        id = bot.state["data"].get("funnel_run_id")
        require(id, "Начните скрининг: /start")
        if not bot.privacy_gate("funnel_resume", {}):
            return True
        run = bot.api("/funnel-runs/" + id + "/resume", "POST", {})
        if run["status"] == "completed":
            result = run["data"]["result"]
            bot.send(
                result["text"] + "\n\n" + result["limitations"],
                [("📋 О диагностике", "funnel_consultation", {"id": id})],
            )
        else:
            show_question(bot, run)
    elif action == "funnel_age":
        group = d.get("age_group")
        entry_id = d.get("entry_id")
        bot.api("/funnel-entries/" + entry_id + "/age", "POST", {"age_group": group})
        bot.state_set(step=None, funnel_age=group, funnel_entry_id=entry_id)
        if group == "outside":
            with connect(bot.app.config["DATABASE"]) as db:
                obj = published(db)
            buttons = []
            if obj and obj["data"].get("contact_url"):
                buttons.append(("💬 Написать Марине", "url", {"url": obj["data"]["contact_url"]}))
            buttons.append(("Выбрать возраст заново", "welcome", {}))
            bot.send(
                "Этот скрининг рассчитан на детей 3–14 лет. Для другого возраста нужна индивидуальная консультация специалиста.",
                buttons,
            )
            return True
        require(group in AGES, "Выберите возраст ребёнка")
        # Keep entry/age in the continuation callback if the privacy policy changes.
        if not bot.privacy_gate("funnel_age", {"entry_id": entry_id, "age_group": group}):
            return True
        run = bot.api(
            "/funnel-runs", "POST", {"age_group": group, "key": "entry:" + entry_id + ":" + group}
        )
        bot.state_set(funnel_run_id=run["id"])
        show_question(bot, run)
    elif action == "funnel_answer":
        if not bot.privacy_gate(
            "funnel_answer", {k: d[k] for k in ("id", "revision", "question", "value")}
        ):
            return True
        run = bot.api(
            "/funnel-runs/" + d["id"] + "/answer",
            "POST",
            {k: d[k] for k in ("revision", "question", "value")},
        )
        show_question(bot, run)
    elif action == "funnel_refuse":
        result = bot.api("/funnel-runs/" + d["id"] + "/refuse", "POST", {})
        bot.send(
            result.get("message", "Предложение закрыто. Результат скрининга сохранён в чате."),
            [("В меню", "menu", {})],
        )
    elif action == "funnel_consultation":
        text = bot.api("/funnel-runs/" + d["id"] + "/consultation")["text"]
        bot.send(text, [("📅 Записаться на диагностику", "funnel_book", {"id": d["id"]})])
    elif action == "funnel_book":
        if not bot.privacy_gate("funnel_book", {"id": d["id"]}):
            return True
        bot.state_set(step="funnel_booking", funnel_run_id=d["id"])
        bot.send(
            "В какие дни и время вам удобно встретиться? Укажите часовой пояс. Специалист согласует время с вами."
        )
    elif action == "funnel_buy":
        if not bot.privacy_gate("funnel_buy", {"id": d["id"]}):
            return True
        order = bot.api("/funnel-runs/" + d["id"] + "/purchase", "POST", {})
        if order["status"] == "paid":
            bot.send("Руководство оплачено. Файл будет доставлен в этот чат.")
            return True
        attempt = bot.api("/orders/" + order["id"] + "/checkout", "POST", {})
        if attempt["status"] == "ready":
            bot.send(
                "Счёт готов. Оплатите в Telegram Stars:",
                [("💳 Оплатить", "url", {"url": attempt["data"]["url"]})],
            )
        else:
            bot.send("Готовлю счёт. Ссылка появится в этом чате.")
    elif action == "funnel_free":
        from .bot import send_file

        response = bot.client.get("/api/funnel-runs/" + d["id"] + "/free-pdf")
        require(response.status_code == 200, (response.json or {}).get("error", "Файл недоступен"))
        send_file(bot.app, bot.chat, "games.pdf", response.data, "application/pdf")
    else:
        return False
    return True
