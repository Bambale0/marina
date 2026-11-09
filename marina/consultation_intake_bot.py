"""Three-question Telegram forms and personal-contact routes."""

from . import require
from .consultation_journey import contact_url, button_text, display_price
from .db import uid


def deliver_route_material(bot, route):
    """Attach a ready route PDF after consent, retaining acknowledged delivery on retry."""
    from .bot import send_file
    from .commerce import policies
    from .consultation_delivery import send_once
    from .consultation_journey import material_ready
    from .db import connect

    with connect(bot.app.config["DATABASE"]) as db:
        policy = policies(db, "consultation_routes")
        material = policy["data"]["routes"].get(route, {}) if policy else {}
        if not material_ready(db, material.get("material_id")):
            return
    choice = {"route": route, "interest": "download"}
    if not bot.privacy_gate("outside_interest", choice):
        return
    lead = bot.api(
        "/consultation-leads",
        "POST",
        {
            "route": route,
            "action": "download",
            "key": "bot-route-material:" + policy["id"] + ":" + route,
        },
    )
    response = bot.client.get("/api/consultation-leads/" + lead["id"] + "/pdf")
    require(
        response.status_code == 200,
        (response.json or {}).get("error", "Материал недоступен"),
    )
    route_snapshot = lead["data"]["route_snapshot"]
    marker = "outside-material:" + lead["id"]
    send_once(
        bot.app,
        marker + ":file",
        lambda: send_file(
            bot.app,
            bot.chat,
            route_snapshot["material_filename"],
            response.data,
            "application/pdf",
        ),
    )
    send_once(bot.app, marker + ":text", lambda: bot.send(route_snapshot["gift_text"]))


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
        + [
            (
                button_text(draft["journey"], "back", "", "❌ Назад к описанию"),
                "funnel_consultation",
                {"id": draft["run_id"]},
            )
        ],
    )


def existing_booking(bot, id):
    bot.state_set(step=None, consultation_intake=None)
    bot.send(
        "Анкета уже отправлена. Для изменения данных свяжитесь со специалистом через вашу запись.",
        [("Открыть запись", "card", {"id": id, "kind": "booking"})],
    )
    return True


def advance(bot, value):
    draft = bot.state["data"].get("consultation_intake")
    require(draft and len(draft["answers"]) < 3, "Откройте актуальную анкету записи")
    q = draft["journey"]["questions"][len(draft["answers"])]
    answers = {**draft["answers"], q["id"]: value}
    bot.state_set(step="consultation_intake", consultation_intake={**draft, "answers": answers})
    if len(answers) < 3:
        return show_question(bot)
    booking = bot.api("/funnel-runs/" + draft["run_id"] + "/book", "POST", {"answers": answers})
    if booking["data"].get("intake_answers") != answers:
        return existing_booking(bot, booking["id"])
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
        if details.get("booking_id"):
            return existing_booking(bot, details["booking_id"])
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
                    button_text(j, "contact_open", "", "👩‍⚕️ Написать Марине в личные сообщения"),
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
        price = display_price(r["amount_rub"] * 100)
        bot.send(
            r["text"].replace("{price}", price),
            [
                (
                    r["booking_label"].replace("{price}", price),
                    "outside_interest",
                    {"route": d["route"], "interest": "booking"},
                ),
                (
                    r.get("contact_label", "💬 Задать вопрос лично специалисту"),
                    "outside_interest",
                    {"route": d["route"], "interest": "contact"},
                ),
                ("⬅️ Другой возраст", "outside_menu", {}),
            ],
        )
        if d["route"] == "baby_0_2":
            deliver_route_material(bot, d["route"])
        elif r.get("material_id"):
            bot.send(
                r["gift_text"],
                [
                    (
                        "📄 Получить чек-лист",
                        "outside_interest",
                        {"route": d["route"], "interest": "download"},
                    )
                ],
            )
    elif action == "outside_interest":
        interest = d.get("interest")
        require(interest in ("contact", "booking", "download"), "Выберите действие")
        choice = {"route": d["route"], "interest": interest}
        if interest == "download":
            deliver_route_material(bot, d["route"])
            return True
        if not bot.privacy_gate("outside_interest", choice):
            return True
        lead = bot.api(
            "/consultation-leads",
            "POST",
            {"route": d["route"], "action": interest, "key": "bot:" + bot.update_id},
        )
        if d["route"] == "baby_0_2" and interest == "booking":
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
