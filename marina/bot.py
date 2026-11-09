"""Telegram polling adapter. Only verified getUpdates data reaches trusted_client."""

import io
import json
import secrets
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import Problem, age_months, object_get, require
from .db import audit, connect, insert, transaction, uid, unpack, update
from .providers import telegram
from . import sessions
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
    ("Мои данные", "privacy_settings", {}),
]

ADMIN_CONTENT_KINDS = [
    ("Сценарии", "funnel"),
    ("Онбординг", "onboarding"),
    ("Решения", "decision"),
    ("PDF и материалы", "material"),
    ("Методики", "method"),
    ("Тарифы и продукты", "product"),
    ("Правила", "policy"),
    ("Упражнения", "exercise"),
    ("Подборки", "selection"),
    ("Курсы", "course"),
    ("Шаблоны и оферта", "template"),
]

ADMIN_KIND_LABELS = {
    **{kind: title for title, kind in ADMIN_CONTENT_KINDS},
    "account": "Аккаунты",
    "booking": "Записи",
    "case": "Разборы",
    "funnel_run": "Запуски сценариев",
    "order": "Заказы",
    "payment_attempt": "Попытки оплаты",
    "payment_event": "События оплаты",
    "purchase_intent": "Намерения покупки",
    "quote": "Расчёты",
    "reconciliation": "Сверки",
    "refund": "Возвраты",
    "report": "Отчёты",
    "right": "Права доступа",
    "video_review": "Видеоразборы",
    "video_task": "Задания с видео",
    "consultation_lead": "Другой возраст",
    "consultation_result": "Персональные результаты консультаций",
}

RELATED_OBJECT_KEYS = {
    "attempt_id",
    "booking_id",
    "case_id",
    "course_id",
    "funnel_id",
    "guide_right_id",
    "material_id",
    "media_id",
    "method_id",
    "order_id",
    "payment_attempt_id",
    "payment_event_id",
    "policy_id",
    "product_id",
    "quote_id",
    "refund_id",
    "report_id",
    "right_id",
    "subject_id",
    "template_id",
}


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
        token = sessions.issue(db, app, account_id, now, 3600)
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

    def cancel_admin_prompt(self):
        if not str(self.state["data"].get("step") or "").startswith("admin_"):
            return
        self.state_set(
            step=None,
            admin_pending=None,
            admin_object_id=None,
            admin_object_revision=None,
            admin_kind=None,
            admin_asset_reason=None,
            object_id=None,
            object_revision=None,
            command=None,
        )

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
                # Keep callback routing separate from a business payload named
                # action (for example consultation route booking/contact).
                # Legacy callbacks without that field retain their stored shape.
                callback_data = dict(data)
                callback_data["_callback_action" if "action" in data else "action"] = action
                obj = insert(
                    db,
                    "bot_callback",
                    callback_data,
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
        if buttons:
            from .scope import action_service, service_enabled

            with connect(self.app.config["DATABASE"]) as db:
                buttons = [
                    b
                    for b in buttons
                    if not action_service(b[1], b[2])
                    or service_enabled(db, action_service(b[1], b[2]))
                ]
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

    def require_admin(self):
        require(
            self.user["id"] in self.app.config["ADMIN_TELEGRAM_IDS"],
            "Недостаточно прав",
            403,
        )

    def admin_menu(self):
        self.require_admin()
        self.cancel_admin_prompt()
        return self.send(
            "Админка Марины в Telegram. Выберите раздел. Все изменения и чувствительные "
            "действия сохраняются с указанной вами причиной в неизменяемом журнале.",
            [
                ("Трипваер", "admin_tripwire", {}),
                ("Клиенты и доступ", "admin_accounts", {}),
                ("Контент и настройки", "admin_content_kinds", {}),
                ("Все сущности", "admin_entities", {}),
                ("Поиск сущности", "admin_search_start", {}),
                ("Очередь", "queue", {}),
                ("Операции и интеграции", "admin_operations", {}),
                ("Журнал аудита", "admin_audit", {}),
            ],
        )

    def admin_content_kinds(self):
        self.require_admin()
        return self.send(
            "Контент и настройки. Значения читаются из сущностей, а не из меню бота.",
            [(title, "admin_content_list", {"kind": kind}) for title, kind in ADMIN_CONTENT_KINDS]
            + [("В админку", "admin_menu", {})],
        )

    def admin_content_list(self, kind, offset=0):
        self.require_admin()
        allowed = {value for _, value in ADMIN_CONTENT_KINDS}
        require(kind in allowed, "Неизвестный тип сущности")
        offset = max(0, int(offset))
        items = self.api("/admin/content?kind=" + kind + "&order=desc&offset=" + str(offset))[
            "items"
        ]
        title = ADMIN_KIND_LABELS[kind]
        buttons = [
            (
                str(o["data"].get("title") or o["id"])[:48] + " · " + o["status"],
                "admin_content_card",
                {"id": o["id"]},
            )
            for o in items[:30]
        ]
        if offset:
            buttons.append(
                ("Назад", "admin_content_list", {"kind": kind, "offset": max(0, offset - 30)})
            )
        if len(items) > 30:
            buttons.append(("Далее", "admin_content_list", {"kind": kind, "offset": offset + 30}))
        buttons += [
            ("Создать", "admin_content_create", {"kind": kind}),
            ("К разделам", "admin_content_kinds", {}),
            ("В админку", "admin_menu", {}),
        ]
        return self.send(
            title
            + ". Показаны записи "
            + str(offset + 1 if items else 0)
            + "–"
            + str(offset + min(30, len(items)))
            + ".",
            buttons,
        )

    def admin_content_card(self, id):
        self.require_admin()
        o = self.api("/admin/content/" + id)
        title = str(o["data"].get("title") or "Без названия")
        text = (
            ADMIN_KIND_LABELS.get(o["kind"], o["kind"])
            + "\n"
            + title
            + "\nСтатус: "
            + o["status"]
            + "\nРедакция: "
            + str(o["revision"])
            + "\nID: "
            + o["id"]
        )
        buttons = [("Скачать JSON", "admin_content_export", {"id": id})]
        buttons.append(("Предпросмотр", "admin_content_preview", {"id": id}))
        if o["status"] == "draft":
            buttons += [
                ("Изменить JSON", "admin_content_edit", {"id": id}),
                ("Утвердить", "admin_content_action", {"id": id, "command": "approve"}),
                ("Удалить черновик", "admin_content_action", {"id": id, "command": "delete"}),
            ]
            if o["kind"] in ("material", "exercise"):
                buttons.append(("Загрузить файл", "admin_asset_start", {"id": id}))
            if o["kind"] == "material" and o["data"].get("text"):
                buttons.append(("Сформировать PDF", "admin_material_render", {"id": id}))
            if o["kind"] == "exercise" and o["data"].get("video_id"):
                buttons += [
                    (
                        "Скачать видео для проверки",
                        "admin_content_asset_download",
                        {"id": id, "asset_id": o["data"]["video_id"]},
                    ),
                    ("Подтвердить просмотр видео", "admin_exercise_verify", {"id": id}),
                ]
        if (
            o["kind"] == "material"
            and o["data"].get("asset_id")
            and o["data"].get("format") == "pdf"
        ):
            buttons.append(("Скачать PDF", "admin_material_pdf", {"id": id}))
        if o["status"] == "approved":
            buttons.append(
                ("Опубликовать", "admin_content_action", {"id": id, "command": "publish"})
            )
        if o["status"] != "draft":
            buttons.append(("Новая версия", "admin_content_action", {"id": id, "command": "copy"}))
        if o["status"] not in ("archived", "deleted"):
            buttons.append(
                ("Архивировать", "admin_content_action", {"id": id, "command": "archive"})
            )
        buttons += [
            ("К списку", "admin_content_list", {"kind": o["kind"]}),
            ("В админку", "admin_menu", {}),
        ]
        return self.send(text, buttons)

    def admin_entity_list(self, kind, offset=0):
        self.require_admin()
        offset = max(0, int(offset))
        items = self.api("/admin/objects?kind=" + kind + "&order=desc&offset=" + str(offset))[
            "items"
        ]
        buttons = [
            (
                str(o["data"].get("title") or o["data"].get("topic") or o["id"])[:48]
                + " · "
                + o["status"],
                "admin_entity_card",
                {"id": o["id"]},
            )
            for o in items[:30]
        ]
        if offset:
            buttons.append(
                ("Назад", "admin_entity_list", {"kind": kind, "offset": max(0, offset - 30)})
            )
        if len(items) > 30:
            buttons.append(("Далее", "admin_entity_list", {"kind": kind, "offset": offset + 30}))
        buttons.append(("Все сущности", "admin_entities", {}))
        return self.send(
            ADMIN_KIND_LABELS.get(kind, kind)
            + ". Показаны записи "
            + str(offset + 1 if items else 0)
            + "–"
            + str(offset + min(30, len(items)))
            + ".",
            buttons,
        )

    def admin_entity_card(self, id):
        self.require_admin()
        o = self.api("/admin/objects/" + id)["object"]
        if o["kind"] in {value for _, value in ADMIN_CONTENT_KINDS}:
            return self.admin_content_card(id)
        return self.admin_card(id)

    def admin_tripwire(self):
        self.require_admin()
        data = self.api("/admin/tripwire")

        def yes(value):
            return "да" if value else "нет"

        lines = [
            "Трипваер",
            "Lava API: " + yes(data["integrations"]["lava_api"]),
            "Lava webhook: " + yes(data["integrations"]["lava_webhook"]),
            "Оплата только через Lava.top: " + yes(data["integrations"]["lava_checkout_enabled"]),
            "Оферта: " + (data["offer"]["title"] if data.get("offer") else "не опубликована"),
            "Правила покупки: "
            + (data["policy"]["title"] if data.get("policy") else "не опубликованы"),
        ]
        buttons = []
        if data.get("offer"):
            buttons.append(("Оферта", "admin_content_card", {"id": data["offer"]["id"]}))
        if data.get("policy"):
            buttons.append(("Правила покупки", "admin_content_card", {"id": data["policy"]["id"]}))
        for group in data["groups"]:
            funnel, product, material = (
                group.get("funnel"),
                group.get("product"),
                group.get("material"),
            )
            ready = bool(
                funnel
                and funnel["status"] == "published"
                and product
                and product["status"] == "published"
                and material
                and material["status"] == "published"
                and material["asset_configured"]
            )
            delay = (
                group.get("funnel", {}).get("tripwire_delay_seconds")
                if group.get("funnel")
                else None
            )
            lines.append(
                group["label"]
                + ": "
                + ("готово" if ready else "неполный комплект")
                + (" · трипваер через " + str(delay) + " сек." if delay is not None else "")
            )
            for key, label in (("funnel", "сценарий"), ("product", "тариф"), ("material", "PDF")):
                if group.get(key):
                    buttons.append(
                        (
                            group["label"] + " · " + label,
                            "admin_content_card",
                            {"id": group[key]["id"]},
                        )
                    )
        buttons.append(("В админку", "admin_menu", {}))
        return self.send("\n".join(lines), buttons)

    def admin_operations(self, offset=0):
        self.require_admin()
        offset = max(0, int(offset))
        data = self.api("/admin/operations?offset=" + str(offset))
        lines = ["Операции и интеграции"]
        for key, configured in data["integrations"].items():
            lines.append(key + ": " + ("настроено" if configured else "не настроено"))
        lines.append(
            "Исходящие события: показаны "
            + str(offset + 1 if data["outbox"] else 0)
            + "–"
            + str(offset + min(30, len(data["outbox"])))
        )
        buttons = [
            (
                str(event["event"])[:36] + " · " + event["status"],
                "admin_outbox_card",
                {"id": event["id"]},
            )
            for event in data["outbox"][:30]
        ]
        if offset:
            buttons.append(("Назад", "admin_operations", {"offset": max(0, offset - 30)}))
        if len(data["outbox"]) > 30:
            buttons.append(("Далее", "admin_operations", {"offset": offset + 30}))
        buttons.append(("В админку", "admin_menu", {}))
        return self.send("\n".join(lines), buttons)

    def admin_outbox_card(self, id):
        self.require_admin()
        event = self.api("/admin/outbox/" + id)
        lines = [
            "Доставка " + event["event"],
            "Статус: " + event["status"],
            "Попыток: " + str(event["attempts"]),
            "ID: " + event["id"],
        ]
        buttons = [
            ("Открыть исходный объект", "admin_entity_card", {"id": event["object_id"]}),
            ("История попыток", "admin_outbox_attempts", {"id": event["id"]}),
        ]
        if event["event"] not in ("invoice.create", "refund.create") and event["status"] in (
            "sent",
            "failed",
        ):
            buttons.append(("Повторить доставку", "admin_outbox_retry", {"id": event["id"]}))
        buttons += [
            ("К операциям", "admin_operations", {}),
            ("В админку", "admin_menu", {}),
        ]
        return self.send("\n".join(lines), buttons)

    def admin_audit(self, offset=0):
        self.require_admin()
        offset = max(0, int(offset))
        items = self.api("/admin/audit?order=desc&offset=" + str(offset))["items"]
        lines = ["Последние действия в журнале аудита:"]
        for item in items[:15]:
            lines.append(
                str(item["id"])
                + " · "
                + item["action"]
                + " · "
                + str(item.get("reason") or "без причины")
                + "\n"
                + str(item.get("object_id") or "")
            )
        if not items:
            lines.append("Журнал пуст.")
        buttons = []
        if offset:
            buttons.append(("Назад", "admin_audit", {"offset": max(0, offset - 15)}))
        if len(items) > 15:
            buttons.append(("Далее", "admin_audit", {"offset": offset + 15}))
        buttons.append(("В админку", "admin_menu", {}))
        return self.send("\n\n".join(lines), buttons)

    def admin_accounts(self, offset=0, query=""):
        self.require_admin()
        offset = max(0, int(offset))
        query = str(query)[:100]
        path = "/admin/accounts?" + urlencode({"q": query, "offset": offset, "order": "desc"})
        items = self.api(path)["items"]
        buttons = [
            (
                str(account.get("name") or account["id"])[:48]
                + (" · ограничен" if account.get("blocked") else " · активен"),
                "admin_account_card",
                {"id": account["id"]},
            )
            for account in items[:30]
        ]
        if offset:
            buttons.append(
                (
                    "Назад",
                    "admin_accounts",
                    {"offset": max(0, offset - 30), "query": query},
                )
            )
        if len(items) > 30:
            buttons.append(("Далее", "admin_accounts", {"offset": offset + 30, "query": query}))
        buttons += [
            ("Найти клиента", "admin_account_search", {}),
            ("В админку", "admin_menu", {}),
        ]
        return self.send("Клиенты. Показано: " + str(min(30, len(items))) + ".", buttons)

    def admin_account_card(self, id):
        self.require_admin()
        card = self.api("/admin/accounts/" + id)
        account = card["account"]
        lines = [
            "Клиент: " + str(account.get("name") or "Без имени"),
            "Статус: " + ("ограничен" if account.get("blocked") else "активен"),
            "Telegram ID: " + str(account["telegram_id"]),
            "Активных сессий: "
            + str(sum(1 for session in card["sessions"] if not session["revoked"])),
            "Объектов: " + str(len(card["objects"])),
            "ID: " + account["id"],
        ]
        buttons = [
            (
                str(o["data"].get("title") or o["data"].get("name") or o["kind"])[:48]
                + " · "
                + o["status"],
                "admin_entity_card",
                {"id": o["id"]},
            )
            for o in card["objects"][:30]
        ]
        buttons += [
            (
                "Восстановить вход" if account.get("blocked") else "Ограничить вход",
                "admin_account_action",
                {"id": account["id"], "command": "restore" if account.get("blocked") else "block"},
            ),
            (
                "Отозвать сессии",
                "admin_account_action",
                {"id": account["id"], "command": "revoke-sessions"},
            ),
            ("К клиентам", "admin_accounts", {}),
            ("В админку", "admin_menu", {}),
        ]
        return self.send("\n".join(lines), buttons)

    def admin_document(self, message, limit):
        document = message.get("document")
        require(document, "Отправьте документ")
        require(document.get("file_size", 0) <= limit, "Файл превышает допустимый размер")
        info = telegram(self.app, "getFile", {"file_id": document["file_id"]})
        path = info.get("file_path", "")
        require(path and ".." not in path and not path.startswith("/"), "Не удалось прочитать файл")
        try:
            if self.app.config.get("TELEGRAM_FILE_TRANSPORT"):
                content = self.app.config["TELEGRAM_FILE_TRANSPORT"](path, limit)
            else:
                with urlopen(
                    "https://api.telegram.org/file/bot" + self.app.config["BOT_TOKEN"] + "/" + path,
                    timeout=20,
                ) as response:
                    content = response.read(limit + 1)
        except Exception:
            raise Problem("Не удалось скачать файл") from None
        require(0 < len(content) <= limit, "Файл превышает допустимый размер")
        return (
            content,
            str(document.get("mime_type") or "application/octet-stream"),
            str(document.get("file_name") or "document"),
        )

    def admin_payload_text(self, message):
        text = message.get("text", "").strip()
        if text:
            return text
        content, _, _ = self.admin_document(message, 1024 * 1024)
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise Problem("JSON-файл должен быть в UTF-8") from None

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
            policy = privacy["policy"]["data"]
            if policy.get("consent_text"):
                send_file(
                    self.app,
                    self.chat,
                    "privacy-policy.txt",
                    policy["terms"].encode(),
                    "text/plain; charset=utf-8",
                )
                send_file(
                    self.app,
                    self.chat,
                    "privacy-consent.txt",
                    policy["consent_text"].encode(),
                    "text/plain; charset=utf-8",
                )
            self.send(
                (
                    "Политика и отдельное согласие — в файлах выше. "
                    "Подтверждаю, что я совершеннолетний участник или законный представитель ребёнка, "
                    "и даю согласие на обработку данных в указанном объёме."
                    if policy.get("consent_text")
                    else policy["terms"]
                ),
                [
                    (
                        "Даю согласие на обработку данных"
                        if policy.get("consent_text")
                        else "Прочитал(а) и принимаю",
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
        # Recover marketing buttons created before callback action precedence was fixed.
        if action == "consent" and type(d.get("accepted")) is bool:
            action = "marketing_choice"
        from .scope import action_service, require_service

        service = action_service(action, d)
        if service:
            with connect(self.app.config["DATABASE"]) as db:
                require_service(db, service)
        from .consultation_bot import handle_action as handle_consultation
        from .consultation_intake_bot import handle_action as handle_intake
        from .video_task_bot import handle_action as handle_video_task
        from .specialist_result_bot import handle_action as handle_specialist_result
        from .funnel_bot import handle_action

        # Publication reads its pending draft before generic navigation clears admin prompts.
        if handle_specialist_result(self, action, d):
            return
        if action.startswith("admin_") or action in ("menu", "queue"):
            self.cancel_admin_prompt()
        if handle_video_task(self, action, d) or handle_intake(self, action, d):
            return
        if handle_consultation(self, action, d):
            return
        if handle_action(self, action, d):
            return
        if action == "privacy_settings":
            self.send(
                "Документы и управление данными. Отзыв согласия не удаляет обязательные финансовые записи автоматически.",
                [
                    ("Политика и отдельное согласие", "privacy_documents", {}),
                    ("Выгрузить мои данные", "privacy_export", {}),
                    ("Отозвать согласие", "privacy_revoke", {}),
                    ("Запросить удаление данных", "privacy_delete", {}),
                    ("В меню", "menu", {}),
                ],
            )
        elif action == "privacy_documents":
            policy = self.api("/privacy")["policy"]
            require(policy, "Политика готовится")
            send_file(
                self.app,
                self.chat,
                "privacy-policy.txt",
                policy["data"]["terms"].encode(),
                "text/plain; charset=utf-8",
            )
            if policy["data"].get("consent_text"):
                send_file(
                    self.app,
                    self.chat,
                    "privacy-consent.txt",
                    policy["data"]["consent_text"].encode(),
                    "text/plain; charset=utf-8",
                )
        elif action == "privacy_export":
            send_file(
                self.app,
                self.chat,
                "my-data.json",
                json.dumps(self.api("/data-export"), ensure_ascii=False, indent=2).encode(),
                "application/json",
            )
        elif action == "privacy_revoke":
            self.api("/consents/revoke", "POST", {})
            self.send(
                "Согласие отозвано. Запрос передан специалисту для проверки оснований хранения и удаления. Уже оплаченные материалы сохраняются."
            )
        elif action == "privacy_delete":
            self.api("/data-requests", "POST", {"type": "delete"})
            self.send(
                "Запрос на удаление принят. Специалист проверит, какие данные могут быть удалены и какие необходимо сохранить по закону."
            )
        elif action == "offer":
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
                "intake_start",
                "outside_interest",
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
        elif action in ("consent", "marketing_choice"):
            # Previously sent marketing buttons inherited action='consent'.
            # Their explicit boolean choice must still work after the update.
            if action == "consent" and type(d.get("accepted")) is not bool:
                self.api(
                    "/consents",
                    "POST",
                    {"policy_id": d["id"], "accepted": True, "representative_confirmed": True},
                )
                privacy = self.api("/privacy")
                if privacy["policy"]["data"].get("require_marketing_consent"):
                    return self.send(
                        privacy["policy"]["data"].get(
                            "marketing_consent_text",
                            "Получать предложения и акции сервиса в Telegram?",
                        )
                        + "\n\nЭто необязательно и не влияет на скрининг, купленные материалы или запись.",
                        [
                            (
                                "Да, получать предложения",
                                "marketing_choice",
                                {**d, "accepted": True},
                            ),
                            (
                                "Нет, продолжить без рекламы",
                                "marketing_choice",
                                {**d, "accepted": False},
                            ),
                        ],
                    )
            else:
                self.api(
                    "/marketing-consents",
                    "POST",
                    {"policy_id": d["id"], "accepted": d.get("accepted") is True},
                )
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
                "intake_start",
                "outside_interest",
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
            from .specialist_result_bot import RESULT_STATES

            items = self.api("/history")["items"]
            items = [
                o
                for o in items
                if o["kind"]
                in (
                    "video_review",
                    "video_task",
                    "consultation_result",
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
                        (
                            "Персональный результат"
                            if o["kind"] == "consultation_result"
                            else o["kind"]
                        )
                        + " · "
                        + (
                            RESULT_STATES.get(o["status"], o["status"])
                            if o["kind"] == "consultation_result"
                            else o["status"]
                        ),
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
            from .funnel import price_text

            items = [
                p
                for p in self.api("/catalog/product")["items"]
                if p["data"]["service"] == d["service"]
                and p["data"]["currency"] == "RUB"
                and p["data"].get("web_sales_approved") is True
                and (not d.get("course_id") or p["data"].get("course_id") == d["course_id"])
            ]
            self.send(
                "Выберите тариф."
                if items
                else "Продажи пока не открыты. Условия и материалы готовятся.",
                [
                    (
                        p["data"]["title"] + " · " + price_text(p),
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
            from .funnel import price_text

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
                + price_text(
                    {"data": {"amount": q["data"]["total"], "currency": q["data"]["currency"]}}
                )
            )
            self.send(text, [("Подтвердить и оплатить", "buy", {"id": q["id"]})])
        elif action == "buy":
            order = self.api(
                "/orders", "POST", {"quote_id": d["id"], "key": "bot-quote:" + d["id"]}
            )
            if order["status"] == "paid":
                self.send("Доступ открыт.", MENU)
                return
            self.state_set(step="funnel_checkout_email", funnel_order_id=order["id"])
            self.send(
                "Укажите email для электронного чека Lava.top. Он используется только для оформления платежа."
            )
        elif action == "booking_preparation":
            pdf = self.client.get("/api/bookings/" + d["id"] + "/preparation")
            require(pdf.status_code == 200, (pdf.json or {}).get("error", "Памятка недоступна"))
            send_file(self.app, self.chat, "preparation.pdf", pdf.data, "application/pdf")
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
            booking = self.api(
                "/bookings/" + d["id"] + "/confirm",
                "POST",
                {"proposal_id": d["proposal_id"]},
            )
            self.send("Время подтверждено.", MENU)
            from .consultation_bot import offer_payment

            offer_payment(self, booking)
        elif action == "admin_menu":
            self.admin_menu()
        elif action == "admin_content_