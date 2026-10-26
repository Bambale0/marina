"""Telegram polling adapter. Only verified getUpdates data reaches trusted_client."""

import hashlib
import io
import json
import secrets
import time
from urllib.request import Request, urlopen

from . import Problem, age_months, object_get, require
from .db import audit, connect, insert, transaction, uid, unpack, update
from .providers import telegram
from .onboarding import AGE_GROUPS, screen
from .intro_activities import ACTIVITY_INTRO

MENU = [
    ("Пройти скрининг", "welcome", {}),
    ("Задание с видео", "video_welcome", {}),
    ("Профили", "profiles", {}),
    ("Скрининги", "methods", {}),
    ("Занятия", "training", {}),
    ("Курсы", "courses", {}),
    ("Консультация", "booking", {}),
    ("Мои обращения", "history", {}),
    ("Оферта", "offer", {}),
]


def trusted_client(app, user):
    """Internal adapter, never an HTTP endpoint; caller has a Bot API update."""
    now = int(app.config["CLOCK"]())
    with transaction(app.config["DATABASE"]) as db:
        row = db.execute("SELECT * FROM accounts WHERE telegram_id=?", (user["id"],)).fetchone()
        role = "admin" if user["id"] in app.config["ADMIN_TELEGRAM_IDS"] else "client"
        if row is None:
            account_id = uid()
            db.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,0,?)",
                (
                    account_id,
                    user["id"],
                    str(user.get("first_name", ""))[:120],
                    role,
                    now,
                ),
            )
        else:
            require(not row["blocked"], "Доступ к аккаунту ограничен", 403)
            account_id = row["id"]
            if row["role"] != role:
                db.execute("UPDATE accounts SET role=? WHERE id=?", (role, account_id))
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,0)",
            (hashlib.sha256(token.encode()).hexdigest(), account_id, now + 3600),
        )
        db.execute("DELETE FROM sessions WHERE expires<?", (now,))
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + token
    return client, account_id


class Bot:
    def __init__(self, app, user, chat, update_id):
        self.app, self.user, self.chat, self.update_id = app, user, chat, str(update_id)
        self.client, self.owner = trusted_client(app, user)
        self.now = int(app.config["CLOCK"]())
        with transaction(app.config["DATABASE"]) as db:
            row = db.execute(
                "SELECT * FROM objects WHERE kind='bot_state' AND owner=?",
                (self.owner,),
            ).fetchone()
            self.state = (
                unpack(row)
                if row
                else insert(db, "bot_state", {}, self.now, self.owner, status="active")
            )

    def api(self, path, method="GET", data=None):
        response = self.client.open("/api" + path, method=method, json=data)
        require(
            response.status_code < 400,
            response.json.get("error", "Не удалось выполнить действие"),
        )
        return response.json

    def state_set(self, **values):
        with transaction(self.app.config["DATABASE"]) as db:
            obj = object_get(db, self.state["id"])
            self.state = update(db, obj, self.now, {**obj["data"], **values})

    def keyboard(self, buttons):
        rows = []
        numbered = []

        def flush_numbered():
            if not numbered:
                return
            row_count = (len(numbered) + 2) // 3
            width, extra = divmod(len(numbered), row_count)
            offset = 0
            for i in range(row_count):
                size = width + (i < extra)
                rows.append(numbered[offset : offset + size])
                offset += size
            numbered.clear()

        with transaction(self.app.config["DATABASE"]) as db:
            for title, action, data in buttons:
                if action == "url":
                    flush_numbered()
                    rows.append([{"text": title, "url": data["url"]}])
                    continue
                obj = insert(
                    db,
                    "bot_callback",
                    {"action": action, **data},
                    self.now,
                    self.owner,
                    status="active",
                )
                button = {"text": title, "callback_data": "b:" + obj["id"]}
                if action in ("choose_concern", "funnel_answer"):
                    numbered.append(button)
                else:
                    flush_numbered()
                    rows.append([button])
        flush_numbered()
        return {"inline_keyboard": rows}

    def send(self, text, buttons=None):
        result = None
        while len(text) > 3800:
            split = text.rfind("\n", 0, 3800)
            if split < 100:
                split = 3800
            result = telegram(self.app, "sendMessage", {"chat_id": self.chat, "text": text[:split]})
            text = text[split:].lstrip("\n")
        data = {"chat_id": self.chat, "text": text}
        if buttons:
            data["reply_markup"] = self.keyboard(buttons)
        return telegram(self.app, "sendMessage", data) or result

    def edit(self, message_id, text, buttons=None):
        data = {"chat_id": self.chat, "message_id": message_id, "text": text}
        if buttons:
            data["reply_markup"] = self.keyboard(buttons)
        return telegram(self.app, "editMessageText", data)

    def funnel_age_buttons(self, entry_id):
        from .funnel_bot import age_buttons

        return age_buttons(entry_id)

    def selected(self):
        me = self.api("/me")
        id = self.state["data"].get("subject_id")
        obj = next(
            (s for s in me["subjects"] if s["id"] == id and s["status"] == "active"),
            None,
        )
        if not obj:
            self.send(
                "Сначала создайте и выберите профиль.",
                [("Мои профили", "profiles", {})],
            )
        return obj

    def profile_menu(self):
        items = self.api("/me")["subjects"]
        group = (
            self.state["data"].get("age_group") if self.state["data"].get("onboarding") else None
        )
        if group:
            _, minimum, maximum = AGE_GROUPS[group]
            items = [
                s
                for s in items
                if s["data"]["kind"] == "child"
                and minimum <= age_months(s["data"]["birth_date"], self.now) <= maximum
            ]
        self.send(
            (
                (
                    (
                        "Спасибо! Ваше наблюдение сохранено.\n\n"
                        if self.state["data"]["concern"].get("observation")
                        else ""
                    )
                    + "Ваш выбор: "
                    + self.state["data"]["concern"]["text"]
                    + "\n\n"
                    if self.state["data"].get("concern")
                    else ""
                )
                + "Теперь выберите профиль ребёнка или добавьте новый. "
                "Я спрошу только имя и дату рождения — возраст нужен для подбора теста."
                if self.state["data"].get("onboarding")
                else "Выберите профиль. Для взрослой ветки ребёнок не требуется."
            ),
            [
                (s["data"]["name"], "select", {"id": s["id"]})
                for s in items
                if s["status"] == "active"
            ]
            + [
                *([] if group else [("Добавить взрослого", "add", {"kind": "adult"})]),
                ("Добавить ребёнка", "add", {"kind": "child"}),
                ("В меню", "menu", {}),
            ],
        )

    def privacy_gate(self, next_action="profiles", choice=None):
        privacy = self.api("/privacy")
        if privacy["accepted"]:
            return True
        offer_response = self.client.get("/api/offer")
        offer = offer_response.get_json() if offer_response.status_code == 200 else None
        if self.app.config.get("DEV_OFFER_BYPASSES_CONSENT", False) and offer and offer["accepted"]:
            return True
        if not privacy["policy"]:
            buttons = [("В меню", "menu", {})]
            if next_action == "submit_review_video":
                buttons.insert(0, ("Прислать видео специалисту", next_action, choice or {}))
            if offer:
                buttons.insert(
                    0, ("Оферта", "offer", {"next_action": next_action, "choice": choice})
                )
            self.send(
                "Специалист готовит условия обработки данных. Ввод данных пока закрыт.",
                buttons,
            )
        else:
            self.send(
                privacy["policy"]["data"]["terms"],
                [
                    (
                        "Прочитал(а) и принимаю",
                        "consent",
                        {
                            "id": privacy["policy"]["id"],
                            "next_action": next_action,
                            "choice": choice,
                        },
                    )
                ],
            )
        return False

    def check_onboarding_age(self, data):
        group = self.state["data"].get("age_group")
        if not self.state["data"].get("onboarding") or not group:
            return
        _, minimum, maximum = AGE_GROUPS[group]
        try:
            months = age_months(data.get("birth_date", ""), self.now)
        except (TypeError, ValueError):
            raise Problem("Неверная дата рождения") from None
        require(
            data.get("kind") == "child" and minimum <= months <= maximum,
            "Дата рождения не соответствует выбранной группе. "
            "Проверьте дату или начните заново: /start",
        )

    def ask_attempt(self, id):
        a = self.api("/attempts/" + id)
        if a["status"] == "completed":
            self.send(
                a["data"]["result"]["text"] + "\n\n" + a["data"]["result"]["limitations"],
                [
                    ("Подробный PDF", "products", {"service": "pdf", "attempt_id": id}),
                    ("Разбор специалиста", "products", {"service": "case"}),
                    ("Встреча", "booking", {}),
                    ("Занятия", "training", {}),
                ],
            )
            return
        for q in a["data"]["method_snapshot"]["questions"]:
            if (
                q.get("when")
                and a["data"]["answers"].get(q["when"]["question"]) != q["when"]["answer"]
            ):
                continue
            if q["id"] not in a["data"]["answers"]:
                self.send(
                    q["text"],
                    [
                        (
                            o["text"],
                            "answer",
                            {"id": id, "question": q["id"], "value": o["id"]},
                        )
                        for o in q["options"]
                    ],
                )
                return
        result = self.api("/attempts/" + id + "/complete", "POST", {"revision": a["revision"]})
        self.ask_attempt(result["id"])

    def after_profile(self):
        if self.state["data"].get("onboarding_next") != "video_review":
            return self.action("methods", {})
        subject = self.selected()
        if not subject:
            return
        review = self.api(
            "/video-reviews",
            "POST",
            {
                "subject_id": subject["id"],
                "concern": self.state["data"]["concern"],
                "key": "review:" + self.state["id"] + ":" + self.update_id,
            },
        )
        self.state_set(
            step="review_video",
            video_review_id=review["id"],
            onboarding=False,
            onboarding_next=None,
        )
        self.send(
            "Пришлите видео выполнения выбранного задания одним сообщением: видео или файл MP4, до 20 МБ "
            "и в пределах лимита из условий обработки данных. Можно добавить пояснение в подписи.\n\n"
            "Видео посмотрит специалист, подготовит мини-заключение и предложит запись на полную диагностику."
        )

    def action(self, action, d):
        from .funnel_bot import handle_action

        if handle_action(self, action, d):
            return
        if action == "offer":
            offer = self.api("/offer")
            send_file(
                self.app,
                self.chat,
                "offer.txt",
                offer["text"].encode("utf-8"),
                "text/plain; charset=utf-8",
            )
            buttons = [
                (
                    "✅ Принять оферту",
                    "offer_accept",
                    {
                        "id": offer["id"],
                        "next_action": d.get("next_action"),
                        "choice": d.get("choice") or {},
                    },
                ),
                ("❌ Отменить", "menu", {}),
            ]
            self.send("Оферта — полный текст в файле выше. Вы можете сохранить его.", buttons)
        elif action == "offer_accept":
            self.api("/offer-acceptances", "POST", {"offer_id": d["id"]})
            if d.get("next_action") in (
                "choose_concern",
                "continue_concern",
                "custom_concern",
                "observe_activity",
                "submit_review_video",
                "crm_booking",
                "funnel_age",
                "funnel_answer",
                "funnel_book",
                "funnel_buy",
                "funnel_accept_dev",
                "funnel_resume",
            ):
                self.action(d["next_action"], d["choice"])
            else:
                self.action("menu", {})
        elif action == "video_welcome":
            self.state_set(
                step=None, onboarding=False, age_group=None, concern=None, onboarding_next=None
            )
            self.send(
                "Привет! 👋 Как здорово, что Вы здесь!\n\n"
                "Добро пожаловать в «Нейроразвитие дома». Давайте познакомимся 💛\n\n"
                "Начнём с теста для Вас или ребёнка? Я проведу Вас шаг за шагом, "
                "а после ответов покажу результат с пояснениями.\n\n"
                "Начнём вместе?",
                [("пройти тесты", "start_test", {})],
            )
        elif action == "start_test":
            self.state_set(
                step=None, onboarding=True, age_group=None, concern=None, onboarding_next=None
            )
            self.send(
                "Сколько лет Вашему ребёнку?",
                [
                    (label, "age_group", {"age_group": group})
                    for group, (label, _, _) in AGE_GROUPS.items()
                ],
            )
        elif action == "age_group":
            group = d.get("age_group")
            with connect(self.app.config["DATABASE"]) as db:
                items = screen(db, group)
            self.state_set(step=None)
            text = "\n".join(f"{i}. {item['text']}" for i, item in enumerate(items, 1))
            self.send(
                f"{AGE_GROUPS[group][0]}\n\nС какой проблемой Вы обращаетесь?\n\n{text}\n\n"
                "Выберите один подходящий вариант кнопкой ниже.",
                [
                    (str(i), "choose_concern", {"age_group": group, "concern": item})
                    for i, item in enumerate(items, 1)
                ]
                + [
                    ("добавить свое", "custom_concern", {"age_group": group}),
                    ("Изменить возраст", "start_test", {}),
                ],
            )
        elif action == "custom_concern":
            group = d.get("age_group")
            require(group in AGE_GROUPS, "Выберите возрастную группу")
            if not self.privacy_gate("custom_concern", {"age_group": group}):
                return
            self.state_set(step="onboarding_custom", onboarding=True, age_group=group, concern=None)
            self.send(
                "С какой проблемой Вы обращаетесь?\n\n"
                "Напишите своими словами одним сообщением. До 1000 символов.",
                [("Вернуться к вариантам", "age_group", {"age_group": group})],
            )
        elif action == "choose_concern":
            require(
                d.get("age_group") in AGE_GROUPS and isinstance(d.get("concern"), dict),
                "Откройте знакомство заново: /start",
            )
            choice = {"age_group": d["age_group"], "concern": d["concern"]}
            self.state_set(step=None)
            next_action = (
                "activity_menu" if d["concern"].get("activities") else "submit_review_video"
            )
            description = d["concern"].get("description", "")
            if description:
                self.send(
                    d["concern"]["text"] + "\n\n" + description,
                    [
                        ("Продолжить", next_action, choice),
                        ("Вернуться к вариантам", "age_group", {"age_group": d["age_group"]}),
                    ],
                )
            else:
                self.action(next_action, choice)
        elif action == "activity_menu":
            require(
                d.get("age_group") in AGE_GROUPS and isinstance(d.get("concern"), dict),
                "Откройте знакомство заново: /start",
            )
            concern = d["concern"]
            choice = {"age_group": d["age_group"], "concern": concern}
            activities = concern.get("activities", [])
            require(len(activities) == 2, "Варианты задания ещё готовятся")
            self.state_set(step=None)
            self.send(
                concern["text"] + "\n\n" + ACTIVITY_INTRO + "\n\n"
                "Выберите одно задание из двух. Все задания подряд проводить не нужно.\n\n"
                + "\n".join(f"Вариант {i}: {a['title']}" for i, a in enumerate(activities, 1)),
                [
                    (f"Вариант {i}", "choose_activity", {**choice, "activity_id": a["id"]})
                    for i, a in enumerate(activities, 1)
                ]
                + [("Вернуться к пояснению", "choose_concern", choice)],
            )
        elif action == "choose_activity":
            require(
                d.get("age_group") in AGE_GROUPS and isinstance(d.get("concern"), dict),
                "Откройте знакомство заново: /start",
            )
            concern = d["concern"]
            activity = next(
                (a for a in concern.get("activities", []) if a["id"] == d.get("activity_id")), None
            )
            require(activity, "Выберите один из предложенных вариантов")
            self.state_set(step=None)
            selected = {
                "age_group": d["age_group"],
                "concern": {**concern, "selected_activity": activity},
            }
            self.send(
                activity["title"]
                + "\n\n"
                + activity["instructions"]
                + "\n\nЧто заметить\n"
                + concern["observation_focus"]
                + ("\n\n" + concern["activity_note"] if concern.get("activity_note") else ""),
                [
                    ("Прислать видео специалисту", "submit_review_video", selected),
                    (
                        "Выбрать другой вариант",
                        "activity_menu",
                        {"age_group": d["age_group"], "concern": concern},
                    ),
                ],
            )
        elif action == "submit_review_video":
            require(
                d.get("age_group") in AGE_GROUPS and isinstance(d.get("concern"), dict),
                "Сначала выберите проблему и задание",
            )
            choice = {"age_group": d["age_group"], "concern": d["concern"]}
            if not self.privacy_gate("submit_review_video", choice):
                return
            self.state_set(step=None, onboarding=True, onboarding_next="video_review", **choice)
            self.profile_menu()
        elif action == "crm_booking":
            review = self.api("/video-reviews/" + d["id"])
            require(review["status"] in ("invited", "booked"), "Приглашение ещё не отправлено")
            if not self.privacy_gate("crm_booking", {"id": d["id"]}):
                return
            self.state_set(step="booking_preferences", crm_review_id=d["id"])
            self.send(
                "Запишем Вас на полную диагностику. Какие дни и время удобны? Укажите часовой пояс."
            )
        elif action == "crm_reschedule":
            self.api("/bookings/" + d["id"] + "/reschedule", "POST", {})
            self.send("Запрос передан. Специалист предложит другое время.", MENU)
        elif action == "observe_activity":
            self.action("submit_review_video", d)
        elif action == "continue_concern":
            require(
                d.get("age_group") in AGE_GROUPS and isinstance(d.get("concern"), dict),
                "Откройте знакомство заново: /start",
            )
            choice = {"age_group": d["age_group"], "concern": d["concern"]}
            if not self.privacy_gate("continue_concern", choice):
                return
            self.state_set(step=None, onboarding=True, **choice)
            with transaction(self.app.config["DATABASE"]) as db:
                audit(
                    db,
                    self.owner,
                    "bot.observation" if d["concern"].get("observation") else "bot.introduction",
                    self.state,
                    self.now,
                    "Пользователь сохранил наблюдение после выбранного задания"
                    if d["concern"].get("observation")
                    else "Пользователь выбрал проблему при знакомстве",
                )
            self.profile_menu()
        elif action == "menu":
            self.state_set(step=None, onboarding=False)
            self.send(
                "Нейроразвитие дома\n\nВыберите следующий шаг. Сервис не ставит диагнозы.",
                MENU,
            )
        elif action == "profiles":
            if not self.privacy_gate():
                return
            self.profile_menu()
        elif action == "consent":
            self.api("/consents", "POST", {"policy_id": d["id"], "accepted": True})
            if d.get("next_action") in (
                "choose_concern",
                "continue_concern",
                "custom_concern",
                "observe_activity",
                "submit_review_video",
                "crm_booking",
                "funnel_age",
                "funnel_answer",
                "funnel_book",
                "funnel_buy",
                "funnel_accept_dev",
                "funnel_resume",
            ):
                self.action(d["next_action"], d["choice"])
            else:
                self.profile_menu()
        elif action == "select":
            subject = self.api("/subjects/" + d["id"])
            self.check_onboarding_age(subject["data"])
            self.state_set(subject_id=d["id"], step=None)
            if self.state["data"].get("onboarding"):
                self.after_profile()
            else:
                self.send("Профиль выбран.", MENU)
        elif action == "add":
            require(
                not (self.state["data"].get("onboarding") and self.state["data"].get("age_group"))
                or d["kind"] == "child",
                "Для этого маршрута выберите профиль ребёнка",
            )
            self.state_set(step="profile_name", profile_kind=d["kind"])
            self.send("Как зовут взрослого или ребёнка?")
        elif action == "methods":
            subject = self.selected()
            if not subject:
                return
            self.state_set(onboarding=False)
            items = self.api("/catalog/method?subject_id=" + subject["id"])["items"]
            self.send(
                "Выберите тест — после ответов покажу результат с пояснениями."
                if items
                else "Спасибо за знакомство! Подходящий тест пока готовится. "
                "Профиль сохранён — когда тест появится, его можно будет найти "
                "в разделе «Скрининги».",
                [(m["data"]["title"], "attempt", {"id": m["id"]}) for m in items]
                + [("В меню", "menu", {})],
            )
        elif action == "attempt":
            s = self.selected()
            if s:
                a = self.api(
                    "/attempts",
                    "POST",
                    {
                        "method_id": d["id"],
                        "subject_id": s["id"],
                        "key": "bot:" + self.update_id,
                    },
                )
                self.ask_attempt(a["id"])
        elif action == "answer":
            a = self.api("/attempts/" + d["id"])
            answers = {**a["data"]["answers"], d["question"]: d["value"]}
            if a["status"] == "draft":
                self.api(
                    "/attempts/" + d["id"],
                    "PATCH",
                    {"revision": a["revision"], "answers": answers},
                )
            self.ask_attempt(d["id"])
        elif action == "resume":
            self.ask_attempt(d["id"])
        elif action == "booking":
            if self.selected():
                self.state_set(step="booking_topic", crm_review_id=None)
                self.send("Кратко опишите, что хотите обсудить на встрече.")
        elif action == "history":
            items = self.api("/history")["items"]
            items = [
                o
                for o in items
                if o["kind"]
                in (
                    "video_review",
                    "report",
                    "case",
                    "booking",
                    "order",
                    "attempt",
                    "lesson",
                    "right",
                )
            ]
            self.send(
                "Ваши обращения и результаты." if items else "Пока нет обращений.",
                [
                    (
                        o["kind"] + " · " + o["status"],
                        "card",
                        {"id": o["id"], "kind": o["kind"]},
                    )
                    for o in items[:30]
                ]
                + [("В меню", "menu", {})],
            )
        elif action == "training":
            if self.selected():
                self.state_set(step="equipment")
                self.send(
                    "Какие предметы есть для занятия? Перечислите через запятую или отправьте «нет»."
                )
        elif action == "lesson":
            self.lesson(d["id"])
        elif action == "lesson_done":
            lesson = self.api("/lessons/" + d["id"])
            self.api(
                "/lessons/" + d["id"] + "/complete",
                "POST",
                {
                    "completed_exercises": [e["id"] for e in lesson["data"]["exercises"]],
                    "feedback": d["feedback"],
                },
            )
            self.send("Занятие сохранено. Спасибо за обратную связь.", MENU)
        elif action == "courses":
            items = self.api("/catalog/course")["items"]
            known = {course["id"] for course in items}
            for item in self.api("/my-courses")["items"]:
                if item["course"]["id"] not in known:
                    items.append(item["course"])
                    known.add(item["course"]["id"])
            self.send(
                "Выберите курс." if items else "Программы курсов ещё готовятся.",
                [(c["data"]["title"], "course", {"id": c["id"]}) for c in items]
                + [("В меню", "menu", {})],
            )
        elif action == "course":
            s = self.selected()
            if not s:
                return
            c = next(
                (c for c in self.api("/catalog/course")["items"] if c["id"] == d["id"]),
                None,
            )
            if c is None:
                c = next(
                    (
                        item["course"]
                        for item in self.api("/my-courses")["items"]
                        if item["course"]["id"] == d["id"] and item["right"]["subject"] == s["id"]
                    ),
                    None,
                )
            require(c, "Курс не найден")
            self.send(
                c["data"]["title"],
                [
                    (
                        lesson["title"],
                        "course_lesson",
                        {"course": c["id"], "lesson": lesson["id"], "subject": s["id"]},
                    )
                    for lesson in c["data"]["lessons"]
                ]
                + [
                    (
                        "Купить доступ",
                        "products",
                        {"service": "course", "course_id": c["id"]},
                    )
                ],
            )
        elif action == "course_lesson":
            lesson = self.api(
                "/courses/"
                + d["course"]
                + "/lessons/"
                + d["lesson"]
                + "?subject_id="
                + d["subject"]
            )
            self.send(
                lesson["title"] + "\n\n" + lesson["text"],
                [("Урок завершён", "course_done", d)],
            )
        elif action == "course_done":
            self.api(
                "/courses/" + d["course"] + "/lessons/" + d["lesson"] + "/complete",
                "POST",
                {"subject_id": d["subject"]},
            )
            self.action("course", {"id": d["course"]})
        elif action == "products":
            items = [
                p
                for p in self.api("/catalog/product")["items"]
                if p["data"]["service"] == d["service"]
                and p["data"]["currency"] == "XTR"
                and (not d.get("course_id") or p["data"].get("course_id") == d["course_id"])
            ]
            self.send(
                "Выберите тариф."
                if items
                else "Продажи пока не открыты. Условия и материалы готовятся.",
                [
                    (
                        p["data"]["title"] + " · " + str(p["data"]["amount"]) + " Stars",
                        "case_prepare"
                        if d["service"] == "case" and not d.get("case_id")
                        else "quote",
                        {
                            "product": p["id"],
                            **{
                                k: v
                                for k, v in d.items()
                                if k in ("attempt_id", "booking_id", "case_id")
                            },
                        },
                    )
                    for p in items
                ]
                + [("В меню", "menu", {})],
            )
        elif action == "case_prepare":
            subject = self.selected()
            if subject:
                case = self.api(
                    "/cases",
                    "POST",
                    {
                        "product_id": d["product"],
                        "subject_id": subject["id"],
                        "key": "bot:" + self.update_id,
                    },
                )
                self.action("case_submit", {"id": case["id"]})
        elif action == "quote":
            s = self.selected()
            if not s:
                return
            q = self.api(
                "/quotes",
                "POST",
                {
                    "product_id": d["product"],
                    "subject_ids": [s["id"]],
                    "surface": "telegram",
                    **{k: v for k, v in d.items() if k in ("attempt_id", "booking_id", "case_id")},
                },
            )
            text = (
                q["data"]["product_snapshot"]["title"]
                + "\n\n"
                + q["data"]["product_snapshot"]["terms"]
                + "\n\nИтого: "
                + str(q["data"]["total"])
                + " Stars"
            )
            self.send(text, [("Подтвердить и оплатить", "buy", {"id": q["id"]})])
        elif action == "buy":
            order = self.api(
                "/orders", "POST", {"quote_id": d["id"], "key": "bot-quote:" + d["id"]}
            )
            p = order["data"]
            telegram(
                self.app,
                "sendInvoice",
                {
                    "chat_id": self.chat,
                    "title": p["product_snapshot"]["title"][:32],
                    "description": p["product_snapshot"]["terms"][:255],
                    "payload": order["id"],
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": [{"label": "Услуга", "amount": p["total"]}],
                },
            )
        elif action == "card":
            self.card(d["id"], d["kind"])
        elif action == "case_submit":
            self.state_set(step="case_problem", case_id=d["id"])
            self.send(
                "Опишите одну проблему и контекст. Если нужны материалы, сначала отправьте видео или аудио следующим сообщением."
            )
        elif action == "followup":
            self.state_set(step="followup", case_id=d["id"])
            self.send(
                "Задайте одно уточнение по той же проблеме. Для новой проблемы нужен отдельный разбор."
            )
        elif action == "booking_confirm":
            self.api(
                "/bookings/" + d["id"] + "/confirm",
                "POST",
                {"proposal_id": d["proposal_id"]},
            )
            self.send("Время подтверждено.", MENU)
        elif action == "queue":
            items = self.api("/admin/queue")["items"]
            self.send(
                "Очередь специалиста." if items else "Очередь пуста.",
                [
                    (o["kind"] + " · " + o["status"], "admin_card", {"id": o["id"]})
                    for o in items[:30]
                ],
            )
        elif action == "admin_card":
            self.admin_card(d["id"])
        elif action == "admin_text":
            self.state_set(step="admin_text", object_id=d["id"], command=d["command"])
            self.send("Введите текст. Он сохранится как новая редакция или ответ с аудитом.")
        elif action == "admin_command":
            o = self.api("/admin/objects/" + d["id"])["object"]
            payload = {
                "revision": o["revision"],
                "reason": "Действие специалиста через Telegram",
                "text_revision": len(o["data"].get("revisions", [])),
            }
            self.api(
                "/admin/" + d["route"] + "/" + d["id"] + "/" + d["command"],
                "POST",
                payload,
            )
            self.admin_card(d["id"])
        else:
            self.send("Откройте актуальное меню.", MENU)

    def lesson(self, id):
        lesson = self.api("/lessons/" + id)
        for ex in lesson["data"]["exercises"]:
            video = self.client.get("/api/lessons/" + id + "/video/" + ex["id"])
            if video.status_code == 200:
                send_file(
                    self.app,
                    self.chat,
                    "exercise.mp4",
                    video.data,
                    "video/mp4",
                    "sendVideo",
                )
            self.send(ex["title"] + "\n\n" + ex["instructions"])
        self.send(
            "Отметьте завершение только после выполнения всех упражнений.",
            [
                (title, "lesson_done", {"id": id, "feedback": feedback})
                for title, feedback in [
                    ("Завершено, подходит", "suitable"),
                    ("Было легко", "easy"),
                    ("Было сложно", "hard"),
                    ("Остановились", "stop"),
                ]
            ],
        )

    def card(self, id, kind):
        if kind == "video_review":
            obj = self.api("/video-reviews/" + id)
            from .crm import STATES

            return self.send(
                STATES[obj["status"]]
                + "\n\n"
                + obj["data"]["problem"]
                + ("\n\n" + obj["data"]["conclusion"] if obj["data"].get("conclusion") else "")
                + ("\n\n" + obj["data"]["invitation"] if obj["data"].get("invitation") else ""),
                [("Записаться на диагностику", "crm_booking", {"id": id})]
                if obj["status"] == "invited"
                else [("В меню", "menu", {})],
            )
        if kind == "attempt":
            return self.ask_attempt(id)
        if kind == "lesson":
            return self.lesson(id)
        items = self.api("/history")["items"]
        obj = next((o for o in items if o["id"] == id), None)
        require(obj, "Объект не найден")
        d = obj["data"]
        if kind == "case":
            if d.get("paid_at") is None:
                self.send(
                    "Разбор ожидает оплаты. Срок начнётся после оплаты и полного комплекта.",
                    [("Оплатить разбор", "products", {"service": "case", "case_id": id})],
                )
                if d.get("submitted_at") is not None:
                    return
            if not d.get("submitted_at"):
                self.send(
                    "Комплект ещё не отправлен специалисту.",
                    [("Подготовить и отправить", "case_submit", {"id": id})],
                )
            else:
                self.send(
                    d.get("answer") or "Разбор у специалиста.",
                    [("Уточнить по той же проблеме", "followup", {"id": id})]
                    if obj["status"] == "answered"
                    else [("В меню", "menu", {})],
                )
            if d.get("followup", {}):
                self.send(d["followup"].get("answer") or "Уточнение принято, ответ готовится.")
        elif kind == "booking":
            text = d["topic"]
            buttons = []
            if obj["status"] == "proposed":
                p = next(p for p in d["proposals"] if p["id"] == d["current_proposal"])
                from datetime import datetime
                from zoneinfo import ZoneInfo

                text += "\n" + datetime.fromtimestamp(
                    p["starts"], ZoneInfo("Europe/Moscow")
                ).strftime("%d.%m.%Y %H:%M МСК")
                buttons = [
                    (
                        "Подтвердить время",
                        "booking_confirm",
                        {"id": id, "proposal_id": p["id"]},
                    )
                ]
            if d.get("connection"):
                text += "\nСсылка встречи: " + d["connection"]
            self.send(text, buttons)
        elif kind == "right" and d.get("service") == "guide":
            pdf = self.client.get("/api/guides/" + id + "/pdf")
            require(pdf.status_code == 200, "Руководство недоступно")
            send_file(self.app, self.chat, "guide.pdf", pdf.data, "application/pdf")
        elif kind == "report":
            if d.get("pdf_id"):
                pdf = self.client.get("/api/reports/" + id + "/pdf")
                require(pdf.status_code == 200, "PDF пока недоступен")
                send_file(self.app, self.chat, "report.pdf", pdf.data, "application/pdf")
            else:
                self.send("Отчёт готовится. Специалист проверит конкретную редакцию до выдачи.")
        else:
            self.send("Статус: " + obj["status"], MENU)

    def admin_card(self, id):
        o = self.api("/admin/objects/" + id)["object"]
        d = o["data"]
        buttons = []
        if o["kind"] == "report":
            buttons = [
                (
                    "Написать редакцию",
                    "admin_text",
                    {"id": id, "command": "reports/revise"},
                ),
                (
                    "Утвердить текущую",
                    "admin_command",
                    {"id": id, "route": "reports", "command": "approve"},
                ),
                (
                    "Опубликовать PDF",
                    "admin_command",
                    {"id": id, "route": "reports", "command": "publish"},
                ),
            ]
        elif o["kind"] == "case":
            buttons = [
                ("Основной ответ", "admin_text", {"id": id, "command": "cases/answer"}),
                (
                    "Ответ на уточнение",
                    "admin_text",
                    {"id": id, "command": "cases/followup-answer"},
                ),
            ]
        elif o["kind"] == "video_review":
            buttons = [
                (
                    "Взять на просмотр",
                    "admin_command",
                    {"id": id, "route": "crm", "command": "take"},
                ),
                (
                    "Подтвердить просмотр видео",
                    "admin_command",
                    {"id": id, "route": "crm", "command": "viewed"},
                ),
                ("Написать мини-заключение", "admin_text", {"id": id, "command": "crm/conclusion"}),
                (
                    "Отправить мини-заключение",
                    "admin_command",
                    {"id": id, "route": "crm", "command": "send-conclusion"},
                ),
                ("Пригласить на диагностику", "admin_text", {"id": id, "command": "crm/invite"}),
            ]
            if d.get("conclusion_draft"):
                self.send("Черновик мини-заключения:\n" + d["conclusion_draft"])
        if o["kind"] == "report" and d.get("revisions"):
            self.send("Текущая редакция для проверки:\n" + d["revisions"][-1]["text"])
        if o["kind"] in ("case", "video_review"):
            self.send("Анкета: " + json.dumps(d.get("answers", {}), ensure_ascii=False))
            media = self.api("/admin/objects/" + id).get("media", [])
            for item in media:
                response = self.client.get("/api/media/" + item["id"])
                if response.status_code == 200:
                    review_video = o["kind"] == "video_review" and item["mime"] == "video/mp4"
                    send_file(
                        self.app,
                        self.chat,
                        "client-video.mp4" if review_video else "material",
                        response.data,
                        item["mime"],
                        "sendVideo" if review_video else "sendDocument",
                    )
        self.send(
            o["kind"]
            + " · "
            + o["status"]
            + "\n"
            + str(d.get("problem") or d.get("topic") or "")
            + "\nID: "
            + id,
            buttons,
        )

    def ask_case_question(self):
        d = self.state["data"]
        case = self.api("/cases/" + d["case_id"])
        questions = case["data"]["product_snapshot"].get("case_questions") or [
            {"id": "context", "text": "Опишите ситуацию", "required": True}
        ]
        index = d.get("case_question_index", 0)
        if index < len(questions):
            self.send(
                questions[index]["text"]
                + (
                    "\nМожно отправить /skip, если ответ необязателен."
                    if not questions[index].get("required", True)
                    else ""
                )
            )
            return
        result = self.api(
            "/cases/" + d["case_id"] + "/submit",
            "POST",
            {"problem": d["case_problem_text"], "answers": d.get("case_answers", {})},
        )
        self.state_set(step=None, case_answers={}, case_problem_text=None)
        if result["status"] == "awaiting_payment":
            self.send(
                "Полный комплект сохранён. Для начала работы специалиста нужна оплата.",
                [("Оплатить разбор", "products", {"service": "case", "case_id": d["case_id"]})],
            )
        else:
            self.send("Полный комплект отправлен специалисту.", MENU)

    def message(self, message):
        text = message.get("text", "").strip()
        if text == "/offer":
            return self.action("offer", {})
        if text.startswith("/start"):
            return self.action("welcome", {})
        if text == "/video":
            return self.action("video_welcome", {})
        if text in ("/continue", "/result"):
            return self.action("funnel_resume", {})
        if text == "/stop":
            self.api("/funnel/stop", "POST", {})
            self.state_set(step=None)
            return self.send(
                "Напоминания и предложения отключены. Результаты и оплаченные материалы сохранятся в чате."
            )
        if text == "/paysupport":
            from .funnel import published

            with connect(self.app.config["DATABASE"]) as db:
                content = published(db)
            if content and content["data"].get("contact_url"):
                return self.send(
                    "По вопросам оплаты напишите Марине. Укажите номер заказа из истории обращений.",
                    [("💬 Поддержка по оплате", "url", {"url": content["data"]["contact_url"]})],
                )
            return self.send(
                "По вопросам оплаты обратитесь к специалисту через меню «Консультация». Укажите номер заказа из истории обращений.",
                [("Консультация", "booking", {}), ("Мои обращения", "history", {})],
            )
        if text == "/menu":
            return self.action("menu", {})
        if text == "/id":
            return self.send("Ваш Telegram ID: " + str(self.user["id"]))
        if text in ("/admin", "/queue"):
            return self.action("queue", {})
        if text == "/materials":
            items = self.api("/admin/content?kind=material")["items"]
            return self.send(
                "Материалы: "
                + str(len(items))
                + ". Для нового исходника отправьте /material. Для полного редактора используйте /login."
            )
        if text == "/material":
            self.api("/admin/content?kind=material")
            self.state_set(step="material_title")
            return self.send("Название исходного материала?")
        if text == "/profiles":
            return self.action("profiles", {})
        if text == "/login":
            code = secrets.token_urlsafe(24)
            with transaction(self.app.config["DATABASE"]) as db:
                db.execute(
                    "INSERT INTO login_codes VALUES(?,?,?)",
                    (
                        hashlib.sha256(code.encode()).hexdigest(),
                        self.owner,
                        self.now + 300,
                    ),
                )
            return self.send(
                "Код входа в кабинет действует 5 минут, один раз. Не передавайте его другим:\n\n"
                + code
            )
        step = self.state["data"].get("step")
        if step == "funnel_checkout_email":
            attempt = self.api(
                "/orders/" + self.state["data"]["funnel_order_id"] + "/checkout",
                "POST",
                {"email": text.strip()},
            )
            self.state_set(step=None, funnel_order_id=None)
            from .funnel_bot import show_checkout

            show_checkout(self, attempt)
            return
        if step == "funnel_booking":
            self.api(
                "/funnel-runs/" + self.state["data"]["funnel_run_id"] + "/book",
                "POST",
                {"preferences": text},
            )
            self.state_set(step=None)
            return self.send("Заявка на диагностику передана. Специалист предложит время.", MENU)
        if step == "review_video":
            attachment = message.get("video") or message.get("document")
            require(
                attachment, "Отправьте видео или файл MP4. Текст можно добавить в подпись к видео."
            )
            privacy = self.api("/privacy")
            require(privacy["accepted"], "Сначала примите актуальные условия обработки данных")
            limit = min(privacy["policy"]["data"].get("media_max_bytes", 0), 20 * 1024 * 1024)
            require(
                limit > 0 and attachment.get("file_size", 0) <= limit,
                "Видео превышает допустимый размер",
            )
            info = telegram(self.app, "getFile", {"file_id": attachment["file_id"]})
            path = info.get("file_path", "")
            require(
                path and ".." not in path and not path.startswith("/"), "Не удалось прочитать видео"
            )
            try:
                if self.app.config.get("TELEGRAM_FILE_TRANSPORT"):
                    content = self.app.config["TELEGRAM_FILE_TRANSPORT"](path, limit)
                else:
                    with urlopen(
                        "https://api.telegram.org/file/bot"
                        + self.app.config["BOT_TOKEN"]
                        + "/"
                        + path,
                        timeout=20,
                    ) as response:
                        content = response.read(limit + 1)
            except Exception:
                raise Problem(
                    "Не удалось скачать видео. Попробуйте отправить его ещё раз."
                ) from None
            require(0 < len(content) <= limit, "Видео превышает допустимый размер")
            result = self.client.post(
                "/api/video-reviews/" + self.state["data"]["video_review_id"] + "/video",
                data={
                    "file": (
                        io.BytesIO(content),
                        "video.mp4",
                        attachment.get("mime_type", "video/mp4"),
                    ),
                    "note": message.get("caption", ""),
                },
            )
            require(
                result.status_code < 400, result.json.get("error", "Не удалось сохранить видео")
            )
            self.state_set(step=None)
            return self.send(
                "Спасибо! Видео получено и передано в очередь специалиста. "
                "Мини-заключение и приглашение на диагностику придут в этот чат.",
                MENU,
            )
        if step in ("case_problem", "case_question") and (
            message.get("video")
            or message.get("audio")
            or message.get("voice")
            or message.get("document")
        ):
            attachment = (
                message.get("video")
                or message.get("audio")
                or message.get("voice")
                or message["document"]
            )
            info = telegram(self.app, "getFile", {"file_id": attachment["file_id"]})
            path = info.get("file_path", "")
            require(
                path and ".." not in path and not path.startswith("/"),
                "Не удалось прочитать вложение",
            )
            with urlopen(
                "https://api.telegram.org/file/bot" + self.app.config["BOT_TOKEN"] + "/" + path,
                timeout=15,
            ) as response:
                content = response.read(20 * 1024 * 1024 + 1)
            require(len(content) <= 20 * 1024 * 1024, "Максимальный размер 20 МБ")
            mime = attachment.get("mime_type", "audio/ogg" if message.get("voice") else "video/mp4")
            result = self.client.post(
                "/api/cases/" + self.state["data"]["case_id"] + "/media",
                data={"file": (io.BytesIO(content), "material", mime)},
            )
            require(
                result.status_code < 400,
                result.json.get("error", "Ошибка сохранения файла"),
            )
            return self.send(
                "Файл сохранён. Теперь отправьте текст проблемы и контекст одним сообщением для передачи полного комплекта."
            )
        if step == "activity_observation":
            require(
                1 <= len(text) <= 3000 and not text.startswith("/"),
                "Запишите наблюдение текстом: от 1 до 3000 символов.",
            )
            self.state_set(onboarding_next="video_review")
            self.action(
                "continue_concern",
                {
                    "age_group": self.state["data"]["age_group"],
                    "concern": {**self.state["data"]["concern"], "observation": text},
                },
            )
        elif step == "onboarding_custom":
            require(
                1 <= len(text) <= 1000 and not text.startswith("/"),
                "Опишите проблему текстом: от 1 до 1000 символов.",
            )
            self.action(
                "choose_concern",
                {
                    "age_group": self.state["data"]["age_group"],
                    "concern": {"id": "custom", "text": text},
                },
            )
        elif step == "profile_name":
            require(1 <= len(text) <= 100, "Введите имя до 100 символов")
            self.state_set(step="profile_birth", profile_name=text)
            self.send("Дата рождения в формате ГГГГ-ММ-ДД, например 2020-05-17.")
        elif step == "material_title":
            self.state_set(step="material_text", material_title=text)
            self.send(
                "Отправьте текст исходника. Он останется черновиком до проверки источника и прав использования."
            )
        elif step == "material_text":
            self.api(
                "/admin/content",
                "POST",
                {
                    "kind": "material",
                    "title": self.state["data"]["material_title"],
                    "data": {
                        "owner": "Специалист",
                        "status": "received",
                        "text": text,
                        "source": "Загружено специалистом через Telegram",
                        "rights": "",
                    },
                    "reason": "Получен исходный материал",
                },
            )
            self.state_set(step=None)
            self.send(
                "Исходник сохранён как черновик. Публикация требует проверки источника и прав."
            )
        elif step == "profile_birth":
            d = self.state["data"]
            self.check_onboarding_age({"birth_date": text, "kind": d["profile_kind"]})
            s = self.api(
                "/subjects",
                "POST",
                {
                    "name": d["profile_name"],
                    "kind": d["profile_kind"],
                    "birth_date": text,
                    "key": "bot:" + self.update_id,
                },
            )
            self.state_set(step=None, subject_id=s["id"])
            if self.state["data"].get("onboarding"):
                self.after_profile()
            else:
                self.send("Профиль создан и выбран.", MENU)
        elif step == "booking_topic":
            self.state_set(step="booking_preferences", topic=text)
            self.send("Какие дни и время вам удобны? Укажите часовой пояс.")
        elif step == "booking_preferences":
            if self.state["data"].get("crm_review_id"):
                self.api(
                    "/video-reviews/" + self.state["data"]["crm_review_id"] + "/book",
                    "POST",
                    {"preferences": text},
                )
                self.state_set(step=None, crm_review_id=None)
                return self.send(
                    "Заявка на полную диагностику передана. Специалист предложит время.", MENU
                )
            s = self.selected()
            if s:
                self.api(
                    "/bookings",
                    "POST",
                    {
                        "subject_id": s["id"],
                        "topic": self.state["data"]["topic"],
                        "preferences": text,
                        "key": "bot:" + self.update_id,
                    },
                )
                self.state_set(step=None)
                self.send("Заявка передана. Специалист предложит время.", MENU)
        elif step == "equipment":
            self.state_set(
                step="constraints",
                equipment=[] if text.lower() == "нет" else [v.strip() for v in text.split(",")],
            )
            self.send(
                "Укажите ограничения, отмеченные специалистом, через запятую. Если их нет, напишите «нет»."
            )
        elif step == "constraints":
            s = self.selected()
            if s:
                lesson = self.api(
                    "/lessons/start",
                    "POST",
                    {
                        "subject_id": s["id"],
                        "equipment": self.state["data"]["equipment"],
                        "constraints": []
                        if text.lower() == "нет"
                        else [v.strip() for v in text.split(",")],
                        "key": "bot:" + self.update_id,
                    },
                )
                self.state_set(step=None)
                self.lesson(lesson["id"])
        elif step == "case_problem":
            require(text, "Опишите проблему текстом")
            self.state_set(
                step="case_question", case_problem_text=text, case_answers={}, case_question_index=0
            )
            self.ask_case_question()
        elif step == "case_question":
            d = self.state["data"]
            case = self.api("/cases/" + d["case_id"])
            questions = case["data"]["product_snapshot"].get("case_questions") or [
                {"id": "context", "text": "Опишите ситуацию", "required": True}
            ]
            index = d.get("case_question_index", 0)
            answers = dict(d.get("case_answers", {}))
            if index < len(questions):
                q = questions[index]
                if text == "/skip":
                    require(not q.get("required", True), "Этот ответ обязателен")
                else:
                    require(text, "Введите ответ")
                    answers[q["id"]] = text
                self.state_set(case_answers=answers, case_question_index=index + 1)
            self.ask_case_question()
        elif step == "followup":
            self.api(
                "/cases/" + self.state["data"]["case_id"] + "/followup",
                "POST",
                {"text": text, "same_problem": True},
            )
            self.state_set(step=None)
            self.send("Уточнение принято. Специалисту отведено 48 часов на ответ.", MENU)
        elif step == "admin_text":
            d = self.state["data"]
            o = self.api("/admin/objects/" + d["object_id"])["object"]
            route, command = d["command"].split("/")
            self.api(
                "/admin/" + route + "/" + o["id"] + "/" + command,
                "POST",
                {
                    "revision": o["revision"],
                    "text": text,
                    "reason": "Работа специалиста через Telegram",
                },
            )
            self.state_set(step=None)
            self.admin_card(o["id"])
        else:
            self.send(
                "Выберите действие в меню. Ваш Telegram ID: " + str(self.user["id"]),
                MENU,
            )


def handle_update(app, update):
    if update.get("pre_checkout_query") or (
        update.get("message", {}).get("successful_payment")
        or update.get("message", {}).get("refunded_payment")
    ):
        require(
            app.config["TELEGRAM_WEBHOOK_SECRET"],
            "Настройте внутренний секрет платёжного обработчика",
        )
        response = app.test_client().post(
            "/api/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": app.config["TELEGRAM_WEBHOOK_SECRET"]},
            json=update,
        )
        require(response.status_code == 200, "Ошибка обработки платёжного события")
        return
    callback = update.get("callback_query")
    message = callback.get("message", {}) if callback else update.get("message", {})
    user = callback.get("from") if callback else message.get("from")
    if not user or user.get("is_bot") or message.get("chat", {}).get("type") != "private":
        return
    try:
        bot = Bot(app, user, message["chat"]["id"], update["update_id"])
    except Problem:
        telegram(
            app,
            "sendMessage",
            {
                "chat_id": message["chat"]["id"],
                "text": "Доступ к аккаунту ограничен. Обратитесь к специалисту.",
            },
        )
        return
    try:
        if callback:
            telegram(app, "answerCallbackQuery", {"callback_query_id": callback["id"]})
            value = callback.get("data", "")
            require(value.startswith("b:"), "Откройте меню заново")
            with connect(app.config["DATABASE"]) as db:
                obj = object_get(db, value[2:], "bot_callback", bot.owner)
            require(
                bot.now - obj["created"] < 7 * 24 * 3600,
                "Кнопка устарела, откройте /start",
            )
            bot.action(obj["data"]["action"], obj["data"])
        else:
            bot.message(message)
    except Problem as error:
        bot.send(error.message, [("В меню", "menu", {})])


def poll(app, once=False):
    require(app.config["BOT_TOKEN"], "Добавьте BOT_TOKEN в окружение")
    # Do not silently remove someone else's webhook or discard pending updates.
    info = telegram(app, "getWebhookInfo", {})
    require(not info.get("url"), "Для polling сначала отключите настроенный webhook")
    while True:
        with connect(app.config["DATABASE"]) as db:
            row = db.execute("SELECT value FROM runtime WHERE key='telegram_offset'").fetchone()
            offset = int(row[0]) if row else 0
        try:
            updates = telegram(
                app,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 5,
                    "allowed_updates": [
                        "message",
                        "callback_query",
                        "pre_checkout_query",
                    ],
                },
            )
            for update_data in updates:
                handle_update(app, update_data)
                with transaction(app.config["DATABASE"]) as db:
                    db.execute(
                        "INSERT INTO runtime VALUES('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(update_data["update_id"] + 1),),
                    )
        except Exception:
            # No update payload, token-bearing URL or client text in logs.
            print("Telegram polling retry", flush=True)
            if once:
                raise
            time.sleep(3)
        if once:
            return


def send_file(app, chat, filename, content, mime, method="sendDocument"):
    """Upload a file to the same authenticated private chat; no public file URL."""
    if app.config.get("FILE_TRANSPORT"):
        return app.config["FILE_TRANSPORT"](chat, filename, content, mime, method)
    boundary = secrets.token_hex(24)
    field = "video" if method == "sendVideo" else "document"
    data = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = Request(
        "https://api.telegram.org/bot" + app.config["BOT_TOKEN"] + "/" + method,
        data=data,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            result = json.load(response)
        require(result.get("ok"), "Не удалось отправить файл")
    except Exception:
        raise Problem("Отправка файла временно недоступна; результат сохранён в кабинете") from None
