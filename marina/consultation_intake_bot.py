"""Three-question Telegram forms and personal-contact routes."""

from . import require
from .consultation_journey import contact_url
from .db import uid


def show_question(bot):
    draft = bot.state["data"]["consultation_intake"]
    q = draft["journey"]["questions"][len(draft["answers"])]
    bot.send(
        q["prompt"],
        [
            (
                o["label"],
                "intake_choice",
                {"draft_id": draft["id"], "question": q["id"], "value": o["id"]},
            )
            for o in q["options"]
        ]
        + [("❌ Назад к описанию", "funnel_consultation", {"id": draft["run_id"]})],
    )


def advance(bot, value):
    draft = bot.state["data"].get("consultation_intake")
    require(draft and len(draft["answers"]) < 3, "Откройте актуальную анкету записи")
    q = draft["journey"]["questions"][len(draft["answers"])]
    answers = {**draft["answers"], q["id"]: value}
    bot.state_set(step="consultation_intake", consultation_intake={**draft, "answers": answers})
    if len(answers) < 3:
        return show_question(bot)
    booking = bot.api("/funnel-runs/" + draft["run_id"] + "/book", "POST", {"answers": answers})
    bot.state_set(step=None, consultation_intake=None)
    if booking["data"].get("payment_timing") == "before_scheduling":
        from .consultation_bot import offer_payment

        return offer_payment(bot, booking)
    return bot.send(
        "Анкета принята! Ваши данные сохранены. Специалист свяжется с вами для согласования времени.",
        [("💬 Задать вопрос лично специалисту", "funnel_contact", {"id": draft["run_id"]})],
    )


def handle_text(bot, text):
    d = bot.state["data"].get("consultation_intake")
    require(d and len(d["answers"]) < 3, "Начните запись заново")
    i = len(d["answers"])
    require(i == 0 or (i == 1 and d.get("custom")), "Выберите ответ кнопкой под вопросом")
    require(
        isinstance(text, str) and 0 < len(text.strip()) <= 2000, "Введите ответ до 2000 символов"
    )
    return advance(bot, ("custom:" if i else "") + text.strip())


def handle_action(bot, action, d):
    if action == "intake_start":
        if not bot.privacy_gate("intake_start", d):
            return True
        details = bot.api("/funnel-runs/" + d["id"] + "/consultation-journey")
        draft = {"id": uid(), "run_id": d["id"], "journey": details["journey"], "answers": {}}
        bot.state_set(step="consultation_intake", consultation_intake=draft)
        show_question(bot)
    elif action == "intake_choice":
        draft = bot.state["data"].get("consultation_intake")
        require(
            bot.state["data"].get("step") == "consultation_intake"
            and draft
            and draft["id"] == d.get("draft_id")
            and len(draft["answers"]) < 3,
            "Эта кнопка устарела. Откройте актуальную анкету",
        )
        q = draft["journey"]["questions"][len(draft["answers"])]
        require(
            q["id"] == d.get("question") and d.get("value") in {o["id"] for o in q["options"]},
            "Выберите ответ на текущий вопрос",
        )
        if d["value"] == "other":
            bot.state_set(consultation_intake={**draft, "custom": True})
            bot.send("Напишите свой вариант одним сообщением:")
        else:
            advance(bot, d["value"])
    elif action == "funnel_contact":
        details = bot.api("/funnel-runs/" + d["id"] + "/consultation-journey")
        j = details["journey"]
        bot.send(
            j["contact_text"].replace("{name}", bot.user.get("first_name", "Здравствуйте")),
            [
                (
                    "👩‍⚕️ Написать Марине в личные сообщения",
                    "url",
                    {"url": contact_url(details["contact_url"], j.get("contact_prefill", ""))},
                )
            ],
        )
    elif action == "outside_menu":
        c = bot.api("/consultation-routes")
        bot.state_set(step=None, consultation_intake=None)
        bot.send(
            c["menu_text"],
            [(r["label"], "outside_route", {"route": key}) for key, r in c["routes"].items()]
            + [("⬅️ Вернуться к выбору 3–14 лет", "welcome", {})],
        )
    elif action == "outside_route":
        c = bot.api("/consultation-routes")
        require(d.get("route") in c["routes"], "Выберите категорию")
        bot.state_set(lead_type=d["route"])
        r = c["routes"][d["route"]]
        price = str(r["amount_rub"]) + " ₽"
        bot.send(
            r["text"].replace("{price}", price),
            [
                (
                    r["booking_label"].replace("{price}", price),
                    "outside_interest",
                    {"route": d["route"], "action": "booking"},
                ),
                (
                    "💬 Задать вопрос лично специалисту",
                    "outside_interest",
                    {"route": d["route"], "action": "contact"},
                ),
                ("⬅️ Другой возраст", "outside_menu", {}),
            ],
        )
        if r.get("material_id"):
            bot.send(
                r["gift_text"],
                [
                    (
                        "📄 Получить чек-лист",
                        "outside_interest",
                        {"route": d["route"], "action": "download"},
                    )
                ],
            )
    elif action == "outside_interest":
        if not bot.privacy_gate("outside_interest", d):
            return True
        lead = bot.api("/consultation-leads", "POST", {**d, "key": "bot:" + bot.update_id})
        if d["action"] == "download":
            from .bot import send_file

            response = bot.client.get("/api/consultation-leads/" + lead["id"] + "/pdf")
            require(
                response.status_code == 200,
                (response.json or {}).get("error", "Материал недоступен"),
            )
            send_file(
                bot.app,
                bot.chat,
                lead["data"]["route_snapshot"]["material_filename"],
                response.data,
                "application/pdf",
            )
        elif d["route"] == "baby_0_2" and d["action"] == "booking":
            task = bot.api("/video-tasks", "POST", {"lead_id": lead["id"]})
            bot.send(
                "Заявка на видеоразбор передана специалисту. Специалист даст задание; снимите его выполнение ребёнком. После просмотра вы получите коррекцию выполнения и рекомендации.",
                [("Открыть видеоразбор", "video_task_open", {"id": task["id"]})],
            )
        else:
            bot.send(
                "Запрос передан специалисту. Откройте личный диалог.",
                [
                    (
                        "👩‍⚕️ Написать Марине",
                        "url",
                        {
                            "url": contact_url(
                                lead["data"]["contact_url"],
                                "Здравствуйте! "
                                + lead["data"]["label"]
                                + ". Хочу уточнить условия консультации.",
                            )
                        },
                    )
                ],
            )
    else:
        return False
    return True
