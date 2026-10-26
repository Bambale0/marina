"""Telegram polling adapter. Only verified getUpdates data reaches trusted_client."""

import hashlib
import io
import json
import secrets
import time
from urllib.parse import urlencode
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

    def require_admin(self):
        require(
            self.user["id"] in self.app.config["ADMIN_TELEGRAM_IDS"],
            "Недостаточно прав",
            403,
        )

    def admin_menu(self):
        self.require_admin()
        if str(self.state["data"].get("step") or "").startswith("admin_"):
            self.state_set(
                step=None,
                admin_pending=None,
                admin_object_id=None,
                admin_object_revision=None,
                admin_kind=None,
            )
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
        elif o["status"] == "approved":
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
            "Dev-оплата: " + yes(data["integrations"]["external_lava_test_mode"]),
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
            lines.append(group["label"] + ": " + ("готово" if ready else "неполный комплект"))
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

    def admin_operations(self):
        self.require_admin()
        data = self.api("/admin/operations")
        lines = ["Операции и интеграции"]
        for key, configured in data["integrations"].items():
            lines.append(key + ": " + ("настроено" if configured else "не настроено"))
        lines.append("Исходящие события: " + str(len(data["outbox"])))
        buttons = [
            (
                str(event["event"])[:36] + " · " + event["status"],
                "admin_outbox_card",
                {"id": event["id"]},
            )
            for event in data["outbox"][:30]
        ]
        buttons.append(("В админку", "admin_menu", {}))
        return self.send("\n".join(lines), buttons)

    def admin_outbox_card(self, id):
        self.require_admin()
        data = self.api("/admin/operations")
        event = next((item for item in data["outbox"] if item["id"] == id), None)
        require(event, "Событие доставки не найдено", 404)
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
        elif action == "admin_menu":
            self.admin_menu()
        elif action == "admin_content_kinds":
            self.admin_content_kinds()
        elif action == "admin_content_list":
            self.admin_content_list(d["kind"], d.get("offset", 0))
        elif action == "admin_content_card":
            self.admin_content_card(d["id"])
        elif action == "admin_content_export":
            self.require_admin()
            o = self.api("/admin/content/" + d["id"])
            send_file(
                self.app,
                self.chat,
                o["kind"] + "-" + o["id"] + ".json",
                json.dumps(
                    {"title": o["data"].get("title", ""), "data": o["data"]},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                "application/json",
            )
            self.admin_content_card(o["id"])
        elif action == "admin_content_preview":
            self.require_admin()
            o = self.api("/admin/content/" + d["id"])
            preview = self.api("/admin/content/" + o["id"] + "/preview")
            send_file(
                self.app,
                self.chat,
                o["kind"] + "-" + o["id"] + "-preview.json",
                json.dumps(preview, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )
            self.admin_content_card(o["id"])
        elif action == "admin_content_create":
            self.require_admin()
            require(
                d["kind"] in {value for _, value in ADMIN_CONTENT_KINDS},
                "Неизвестный тип сущности",
            )
            self.state_set(step="admin_content_create_payload", admin_kind=d["kind"])
            self.send(
                "Отправьте JSON текстом или файлом .json: "
                '{"title":"Название","data":{...}}. Затем бот отдельно запросит причину.'
            )
        elif action == "admin_content_edit":
            self.require_admin()
            o = self.api("/admin/content/" + d["id"])
            require(o["status"] == "draft", "Изменять можно только черновик")
            self.state_set(
                step="admin_content_edit_payload",
                admin_object_id=o["id"],
                admin_object_revision=o["revision"],
            )
            self.send(
                "Отправьте полную новую редакцию JSON текстом или файлом .json: "
                '{"title":"Название","data":{...}}. Затем бот отдельно запросит причину.'
            )
        elif action == "admin_content_action":
            self.require_admin()
            require(
                d["command"] in ("copy", "approve", "publish", "archive", "delete"),
                "Действие недоступно",
            )
            current = self.api("/admin/content/" + d["id"])
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "content_action",
                    "id": current["id"],
                    "revision": current["revision"],
                    "command": d["command"],
                },
            )
            self.send("Укажите причину действия. Она сохранится в журнале аудита.")
        elif action == "admin_asset_start":
            self.require_admin()
            current = self.api("/admin/content/" + d["id"])
            require(
                current["kind"] in ("exercise", "material") and current["status"] == "draft",
                "Файл можно добавить только к черновику материала или упражнения",
            )
            self.state_set(
                step="admin_asset_reason",
                admin_pending={
                    "type": "content_asset",
                    "id": current["id"],
                    "kind": current["kind"],
                    "revision": current["revision"],
                },
            )
            self.send("Сначала укажите причину загрузки файла.")
        elif action == "admin_material_render":
            self.require_admin()
            current = self.api("/admin/content/" + d["id"])
            require(current["kind"] == "material", "Материал не найден")
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "material_render",
                    "id": current["id"],
                    "revision": current["revision"],
                },
            )
            self.send("Укажите причину формирования PDF. Она сохранится в аудите.")
        elif action == "admin_material_pdf":
            self.require_admin()
            current = self.api("/admin/content/" + d["id"])
            require(current["kind"] == "material", "Материал не найден")
            response = self.client.get("/api/admin/materials/" + current["id"] + "/pdf")
            require(response.status_code == 200, "PDF не прикреплён")
            send_file(
                self.app,
                self.chat,
                str(current["data"].get("filename") or "material.pdf"),
                response.data,
                "application/pdf",
            )
            self.admin_content_card(current["id"])
        elif action == "admin_content_asset_download":
            self.require_admin()
            current = self.api("/admin/content/" + d["id"])
            require(
                current["kind"] == "exercise" and current["data"].get("video_id") == d["asset_id"],
                "Видео не относится к этой редакции упражнения",
            )
            response = self.client.get("/api/media/" + d["asset_id"])
            require(response.status_code == 200, "Файл недоступен")
            send_file(
                self.app,
                self.chat,
                "exercise.mp4",
                response.data,
                response.content_type or "video/mp4",
                "sendVideo",
            )
            self.admin_content_card(current["id"])
        elif action == "admin_exercise_verify":
            self.require_admin()
            current = self.api("/admin/content/" + d["id"])
            require(
                current["kind"] == "exercise"
                and current["status"] == "draft"
                and current["data"].get("video_id"),
                "Сначала загрузите видео упражнения",
            )
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "content_patch",
                    "id": current["id"],
                    "revision": current["revision"],
                    "title": current["data"].get("title", ""),
                    "data": {**current["data"], "video_verified": True},
                },
            )
            self.send("Укажите причину подтверждения просмотра видео.")
        elif action == "admin_accounts":
            self.admin_accounts(d.get("offset", 0), d.get("query", ""))
        elif action == "admin_account_card":
            self.admin_account_card(d["id"])
        elif action == "admin_account_search":
            self.require_admin()
            self.state_set(step="admin_account_search")
            self.send("Введите имя клиента или ID аккаунта.")
        elif action == "admin_account_action":
            self.require_admin()
            require(d["command"] in ("block", "restore", "revoke-sessions"), "Действие недоступно")
            self.api("/admin/accounts/" + d["id"])
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "account_action",
                    "id": d["id"],
                    "command": d["command"],
                },
            )
            self.send("Укажите причину действия с доступом клиента.")
        elif action == "admin_search_start":
            self.require_admin()
            self.state_set(step="admin_search")
            self.send(
                "Отправьте точный ID сущности или JSON-фильтр: "
                '{"kind":"order","status":"pending","owner":"ID"}. '
                'Для контента можно передать {"kind":"material","q":"текст"}.'
            )
        elif action == "admin_entities":
            self.require_admin()
            metrics = self.api("/admin/operations")["metrics"]
            internal = {"bot_callback", "bot_state", "idempotency"}
            counts = {}
            for item in metrics:
                if item["kind"] not in internal:
                    counts[item["kind"]] = counts.get(item["kind"], 0) + item["count"]
            lines = ["Все доменные сущности:"] + [
                ADMIN_KIND_LABELS.get(kind, kind) + ": " + str(count)
                for kind, count in sorted(counts.items())
            ]
            buttons = [
                (
                    ADMIN_KIND_LABELS.get(kind, kind) + " · " + str(count),
                    "admin_entity_list",
                    {"kind": kind},
                )
                for kind, count in sorted(counts.items())[:50]
            ]
            buttons.append(("В админку", "admin_menu", {}))
            self.send("\n".join(lines), buttons)
        elif action == "admin_entity_list":
            self.admin_entity_list(d["kind"], d.get("offset", 0))
        elif action == "admin_entity_card":
            self.admin_entity_card(d["id"])
        elif action == "admin_object_export":
            self.require_admin()
            card = self.api("/admin/objects/" + d["id"])
            o = card["object"]
            send_file(
                self.app,
                self.chat,
                o["kind"] + "-" + o["id"] + ".json",
                json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )
            self.admin_entity_card(o["id"])
        elif action == "admin_object_versions":
            self.require_admin()
            o = self.api("/admin/objects/" + d["id"])["object"]
            versions = self.api("/admin/objects/" + o["id"] + "/versions")["items"]
            send_file(
                self.app,
                self.chat,
                o["kind"] + "-" + o["id"] + "-versions.json",
                json.dumps(versions, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )
            self.admin_entity_card(o["id"])
        elif action == "admin_object_audit":
            self.require_admin()
            o = self.api("/admin/objects/" + d["id"])["object"]
            offset = max(0, int(d.get("offset", 0)))
            items = self.api(
                "/admin/audit?object_id=" + o["id"] + "&order=desc&offset=" + str(offset)
            )["items"]
            lines = ["Аудит объекта " + o["id"]]
            for item in items[:30]:
                lines.append(
                    str(item["id"])
                    + " · "
                    + item["action"]
                    + " · "
                    + str(item.get("reason") or "без причины")
                )
            if not items:
                lines.append("Записей нет.")
            buttons = []
            if offset:
                buttons.append(
                    (
                        "Назад",
                        "admin_object_audit",
                        {"id": o["id"], "offset": max(0, offset - 30)},
                    )
                )
            if len(items) > 30:
                buttons.append(
                    ("Далее", "admin_object_audit", {"id": o["id"], "offset": offset + 30})
                )
            buttons += [
                ("К карточке", "admin_entity_card", {"id": o["id"]}),
                ("В админку", "admin_menu", {}),
            ]
            self.send(
                "\n\n".join(lines),
                buttons,
            )
        elif action == "admin_tripwire":
            self.admin_tripwire()
        elif action == "admin_operations":
            self.admin_operations()
        elif action == "admin_outbox_card":
            self.admin_outbox_card(d["id"])
        elif action == "admin_outbox_attempts":
            self.require_admin()
            items = self.api("/admin/outbox/" + d["id"] + "/attempts")["items"]
            send_file(
                self.app,
                self.chat,
                "delivery-" + d["id"] + "-attempts.json",
                json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )
            self.admin_outbox_card(d["id"])
        elif action == "admin_outbox_retry":
            self.require_admin()
            self.admin_outbox_card(d["id"])
            self.state_set(
                step="admin_reason",
                admin_pending={"type": "outbox_retry", "id": d["id"]},
            )
            self.send("Укажите причину повторной доставки.")
        elif action == "admin_audit":
            self.admin_audit(d.get("offset", 0))
        elif action == "queue":
            self.require_admin()
            items = self.api("/admin/queue")["items"]
            self.send(
                "Очередь специалиста." if items else "Очередь пуста.",
                [
                    (o["kind"] + " · " + o["status"], "admin_card", {"id": o["id"]})
                    for o in items[:30]
                ],
            )
        elif action == "admin_card":
            self.require_admin()
            self.admin_card(d["id"])
        elif action == "admin_text":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            self.state_set(
                step="admin_text",
                object_id=current["id"],
                object_revision=current["revision"],
                command=d["command"],
            )
            self.send("Введите текст. После этого бот отдельно запросит причину действия.")
        elif action == "admin_object_input":
            self.require_admin()
            require(d["form"] in ("booking", "text", "provider_id"), "Форма недоступна")
            current = self.api("/admin/objects/" + d["id"])["object"]
            self.state_set(
                step="admin_object_input",
                admin_pending={
                    "type": "object_action_extra",
                    "id": current["id"],
                    "revision": current["revision"],
                    "route": d["route"],
                    "command": d["command"],
                    "form": d["form"],
                },
            )
            if d["form"] == "booking":
                self.send(
                    "Отправьте JSON: "
                    '{"starts":1893456000,"duration_minutes":60,"timezone":"Europe/Moscow"}'
                )
            elif d["form"] == "provider_id":
                self.send("Введите ID внешнего счёта Lava.top.")
            else:
                self.send("Введите текст или HTTPS-ссылку для операции.")
        elif action == "admin_refund":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            require(current["kind"] == "order", "Заказ не найден")
            require(current["revision"] == d["revision"], "Заказ изменён", 409)
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "refund",
                    "id": current["id"],
                    "revision": current["revision"],
                    "charge": d["charge"],
                    "amount": d["amount"],
                },
            )
            self.send("Укажите причину полного возврата. Операция будет записана в аудит.")
        elif action == "admin_command_data":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            require(isinstance(d.get("data"), dict), "Данные операции недоступны")
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "object_action_extra",
                    "id": current["id"],
                    "revision": current["revision"],
                    "route": d["route"],
                    "command": d["command"],
                    "extra": d["data"],
                },
            )
            self.send("Укажите причину действия. Она сохранится в журнале аудита.")
        elif action == "admin_subject_edit":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            require(current["kind"] == "subject", "Профиль не найден")
            self.state_set(
                step="admin_subject_edit",
                admin_pending={
                    "type": "subject_edit",
                    "id": current["id"],
                    "revision": current["revision"],
                },
            )
            self.send(
                "Отправьте JSON с изменяемыми полями: "
                '{"name":"Имя","birth_date":"2020-05-17","archive":false}.'
            )
        elif action == "admin_right_grant":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            require(current["kind"] == "subject", "Профиль не найден")
            self.state_set(
                step="admin_right_grant",
                admin_pending={
                    "type": "right_grant",
                    "subject_id": current["id"],
                    "key": "bot-right:" + secrets.token_urlsafe(16),
                },
            )
            self.send(
                "Отправьте JSON: "
                '{"service":"subscription","course_id":null,"starts":1800000000,"ends":1800086400}.'
            )
        elif action == "admin_command":
            self.require_admin()
            current = self.api("/admin/objects/" + d["id"])["object"]
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "object_action",
                    "id": current["id"],
                    "revision": current["revision"],
                    "text_revision": len(current["data"].get("revisions", [])),
                    "route": d["route"],
                    "command": d["command"],
                },
            )
            self.send("Укажите причину действия. Она сохранится в журнале аудита.")
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
        card = self.api("/admin/objects/" + id)
        o = card["object"]
        d = o["data"]
        buttons = []
        if o["kind"] == "subject":
            buttons = [
                ("Изменить профиль", "admin_subject_edit", {"id": id}),
                ("Оформить компенсационный доступ", "admin_right_grant", {"id": id}),
            ]
        elif o["kind"] == "report":
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
                    "Назначить себе",
                    "admin_command_data",
                    {
                        "id": id,
                        "route": "crm",
                        "command": "assign",
                        "data": {"assigned_to": self.owner},
                    },
                ),
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
                (
                    "Добавить внутренний комментарий",
                    "admin_object_input",
                    {"id": id, "route": "crm", "command": "comment", "form": "text"},
                ),
                (
                    "Закрыть обращение",
                    "admin_command",
                    {"id": id, "route": "crm", "command": "close"},
                ),
            ]
            if d.get("conclusion_draft"):
                self.send("Черновик мини-заключения:\n" + d["conclusion_draft"])
        elif o["kind"] == "booking":
            buttons = [
                (
                    "Предложить время",
                    "admin_object_input",
                    {"id": id, "route": "bookings", "command": "propose", "form": "booking"},
                ),
                (
                    "Сохранить ссылку встречи",
                    "admin_object_input",
                    {"id": id, "route": "bookings", "command": "connection", "form": "text"},
                ),
                (
                    "Завершить встречу",
                    "admin_command",
                    {"id": id, "route": "bookings", "command": "complete"},
                ),
                (
                    "Отменить встречу",
                    "admin_command",
                    {"id": id, "route": "bookings", "command": "cancel"},
                ),
            ]
        elif o["kind"] == "reconciliation":
            buttons = [
                (
                    "Зафиксировать решение",
                    "admin_command",
                    {"id": id, "route": "reconciliations", "command": "resolve"},
                )
            ]
        elif o["kind"] == "right":
            buttons = [
                (
                    "Отозвать право",
                    "admin_command",
                    {"id": id, "route": "rights", "command": "revoke"},
                )
            ]
        elif o["kind"] == "payment_attempt":
            buttons = [
                (
                    "Сверить Lava.top",
                    "admin_command",
                    {"id": id, "route": "payments", "command": "reconcile"},
                ),
                (
                    "Привязать ID счёта",
                    "admin_object_input",
                    {
                        "id": id,
                        "route": "payments",
                        "command": "attach-invoice",
                        "form": "provider_id",
                    },
                ),
            ]
        elif o["kind"] in ("funnel_run", "funnel_entry"):
            buttons = [
                (
                    "Остановить напоминания",
                    "admin_command",
                    {
                        "id": id,
                        "route": "funnel-runs" if o["kind"] == "funnel_run" else "funnel-entries",
                        "command": "stop",
                    },
                )
            ]
        elif o["kind"] == "data_request":
            buttons = [
                (
                    title,
                    "admin_command",
                    {"id": id, "route": "data-requests", "command": command},
                )
                for title, command in (
                    ("Взять в работу", "in_progress"),
                    ("Ожидает политики", "awaiting_policy"),
                    ("Отклонить", "reject"),
                )
            ]
        elif o["kind"] == "ai_job":
            buttons = [
                (
                    "Отменить задание",
                    "admin_command",
                    {"id": id, "route": "jobs", "command": "cancel"},
                )
            ]
        if o["kind"] == "case":
            buttons += [
                (
                    "Исправить основной ответ",
                    "admin_text",
                    {"id": id, "command": "cases/correct-answer"},
                ),
                (
                    "Запросить сведения",
                    "admin_text",
                    {"id": id, "command": "cases/request-info"},
                ),
            ]
        if o["kind"] == "order":
            for receipt in card["receipts"]:
                if receipt["provider"] == "stars":
                    buttons.append(
                        (
                            "Полный возврат " + str(receipt["amount"]) + " " + receipt["currency"],
                            "admin_refund",
                            {
                                "id": id,
                                "revision": o["revision"],
                                "charge": receipt["charge"],
                                "amount": receipt["amount"],
                            },
                        )
                    )
        if o["kind"] == "report" and d.get("revisions"):
            self.send("Текущая редакция для проверки:\n" + d["revisions"][-1]["text"])
        if o["kind"] in ("case", "video_review"):
            self.send("Анкета: " + json.dumps(d.get("answers", {}), ensure_ascii=False))
            media = card.get("media", [])
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
        if o.get("owner"):
            buttons.append(("Аккаунт владельца", "admin_account_card", {"id": o["owner"]}))
        if o.get("subject"):
            buttons.append(("Профиль", "admin_entity_card", {"id": o["subject"]}))
        related = []
        for key in RELATED_OBJECT_KEYS:
            value = d.get(key)
            if isinstance(value, str) and value and value != id:
                related.append((key, value))
        for fulfillment in card.get("fulfillments", []):
            value = fulfillment.get("object_id")
            if value and value != id:
                related.append(("fulfillment", value))
        seen = set()
        for label, value in related:
            if value in seen:
                continue
            seen.add(value)
            buttons.append(("Открыть " + label, "admin_entity_card", {"id": value}))
        buttons += [
            ("Скачать карточку JSON", "admin_object_export", {"id": id}),
            ("История изменений", "admin_object_versions", {"id": id}),
            ("Аудит объекта", "admin_object_audit", {"id": id}),
            ("К списку", "admin_entity_list", {"kind": o["kind"]}),
            ("В админку", "admin_menu", {}),
        ]
        self.send(
            o["kind"]
            + " · "
            + o["status"]
            + "\n"
            + str(d.get("problem") or d.get("topic") or "")
            + "\nID: "
            + id
            + "\nПоступлений: "
            + str(len(card.get("receipts", [])))
            + " · доставок: "
            + str(len(card.get("deliveries", [])))
            + " · исполнений: "
            + str(len(card.get("fulfillments", []))),
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
        current_step = str(self.state["data"].get("step") or "")
        if text.startswith("/") and current_step.startswith("admin_"):
            self.state_set(
                step=None,
                admin_pending=None,
                admin_object_id=None,
                admin_object_revision=None,
                admin_kind=None,
                object_id=None,
                object_revision=None,
                command=None,
            )
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
        if text == "/admin":
            return self.admin_menu()
        if text == "/queue":
            return self.action("queue", {})
        if text == "/materials":
            items = self.api("/admin/content?kind=material")["items"]
            return self.send(
                "Материалы: "
                + str(len(items))
                + ". Для нового исходника отправьте /material или откройте /admin."
            )
        if text == "/material":
            self.api("/admin/content?kind=material")
            self.state_set(step="material_title")
            return self.send("Название исходного материала?")
        if text == "/profiles":
            return self.action("profiles", {})
        if text == "/login":
            self.require_admin()
            return self.send(
                "Mini App не используется. Управление доступно прямо в Telegram.",
                [("Открыть админку", "admin_menu", {})],
            )
        step = self.state["data"].get("step")
        if step == "admin_account_search":
            self.require_admin()
            require(1 <= len(text) <= 100, "Введите имя или ID до 100 символов")
            self.state_set(step=None)
            return self.admin_accounts(query=text)
        if step == "admin_search":
            self.require_admin()
            require(text, "Введите ID или JSON-фильтр")
            if not text.startswith("{"):
                self.state_set(step=None)
                return self.admin_entity_card(text)
            try:
                filters = json.loads(text)
            except ValueError:
                raise Problem("Не удалось разобрать JSON-фильтр") from None
            require(isinstance(filters, dict), "Ожидается JSON-объект")
            require(
                set(filters) <= {"kind", "status", "owner", "q"},
                "Разрешены только kind, status, owner и q",
            )
            for value in filters.values():
                require(isinstance(value, str) and len(value) <= 200, "Неверное значение фильтра")
            self.state_set(step=None)
            if filters.get("q"):
                require(filters.get("kind"), "Для поиска контента укажите kind")
                items = self.api(
                    "/admin/content?"
                    + urlencode(
                        {
                            "kind": filters["kind"],
                            "q": filters["q"],
                            "order": "desc",
                        }
                    )
                )["items"]
                return self.send(
                    "Найдено: " + str(len(items)) + ".",
                    [
                        (
                            str(o["data"].get("title") or o["id"])[:48],
                            "admin_content_card",
                            {"id": o["id"]},
                        )
                        for o in items[:30]
                    ]
                    + [("В админку", "admin_menu", {})],
                )
            items = self.api(
                "/admin/objects?"
                + urlencode(
                    {
                        **{key: filters.get(key, "") for key in ("kind", "status", "owner")},
                        "order": "desc",
                    }
                )
            )["items"]
            return self.send(
                "Найдено: " + str(len(items)) + ".",
                [
                    (
                        str(o["data"].get("title") or o["data"].get("name") or o["id"])[:48],
                        "admin_entity_card",
                        {"id": o["id"]},
                    )
                    for o in items[:30]
                ]
                + [("В админку", "admin_menu", {})],
            )
        if step == "admin_asset_reason":
            self.require_admin()
            require(3 <= len(text) <= 1000, "Причина должна содержать от 3 до 1000 символов")
            self.state_set(step="admin_asset_file", admin_asset_reason=text)
            return self.send("Теперь отправьте файл документом. Максимальный размер — 20 МБ.")
        if step == "admin_asset_file":
            self.require_admin()
            pending = self.state["data"].get("admin_pending") or {}
            current = self.api("/admin/content/" + pending.get("id", ""))
            require(
                current["revision"] == pending.get("revision") and current["status"] == "draft",
                "Черновик изменён. Откройте карточку и повторите загрузку.",
                409,
            )
            content, mime, filename = self.admin_document(message, 20 * 1024 * 1024)
            result = self.client.post(
                "/api/admin/content/" + current["id"] + "/asset",
                data={
                    "file": (io.BytesIO(content), filename, mime),
                    "reason": self.state["data"]["admin_asset_reason"],
                },
            )
            require(result.status_code < 400, result.json.get("error", "Не удалось сохранить файл"))
            asset = result.json
            data = {**current["data"]}
            if current["kind"] == "exercise":
                data.update(video_id=asset["id"], video_verified=False)
            else:
                data["asset_id"] = asset["id"]
            updated = self.api(
                "/admin/content/" + current["id"],
                "PATCH",
                {
                    "revision": current["revision"],
                    "title": current["data"].get("title", ""),
                    "data": data,
                    "reason": self.state["data"]["admin_asset_reason"],
                },
            )
            self.state_set(step=None, admin_pending=None, admin_asset_reason=None)
            return self.admin_content_card(updated["id"])
        if step in ("admin_subject_edit", "admin_right_grant"):
            self.require_admin()
            try:
                values = json.loads(text)
            except ValueError:
                raise Problem("Не удалось разобрать JSON") from None
            require(isinstance(values, dict), "Ожидается JSON-объект")
            pending = self.state["data"].get("admin_pending") or {}
            if step == "admin_subject_edit":
                require(
                    set(values) <= {"name", "birth_date", "archive"} and values,
                    "Разрешены name, birth_date и archive",
                )
            else:
                require(
                    set(values) == {"service", "course_id", "starts", "ends"},
                    "Нужны service, course_id, starts и ends",
                )
            self.state_set(step="admin_reason", admin_pending={**pending, "values": values})
            return self.send("Укажите причину действия. Она сохранится в журнале аудита.")
        if step == "admin_object_input":
            self.require_admin()
            pending = self.state["data"].get("admin_pending") or {}
            form = pending.get("form")
            if form == "booking":
                try:
                    extra = json.loads(text)
                except ValueError:
                    raise Problem("Не удалось разобрать JSON") from None
                require(isinstance(extra, dict), "Ожидается JSON-объект")
                require(
                    set(extra) == {"starts", "duration_minutes", "timezone"},
                    "Нужны starts, duration_minutes и timezone",
                )
            elif form == "provider_id":
                require(1 <= len(text) < 200, "Введите ID внешнего счёта")
                extra = {"provider_id": text}
            else:
                require(text, "Введите текст")
                extra = {"text": text}
            self.state_set(
                step="admin_reason",
                admin_pending={**pending, "extra": extra},
            )
            return self.send("Укажите причину действия. Она сохранится в журнале аудита.")
        if step in ("admin_content_create_payload", "admin_content_edit_payload"):
            raw = self.admin_payload_text(message)
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                raise Problem("Не удалось разобрать JSON") from None
            require(isinstance(payload, dict), "Ожидается JSON-объект")
            require(
                isinstance(payload.get("title"), str) and payload["title"].strip(),
                "В JSON нужно непустое поле title",
            )
            require(isinstance(payload.get("data"), dict), "В JSON нужно поле data с объектом")
            pending = {
                "type": "content_create"
                if step == "admin_content_create_payload"
                else "content_edit",
                "title": payload["title"].strip(),
                "data": payload["data"],
            }
            if step == "admin_content_create_payload":
                pending["kind"] = self.state["data"]["admin_kind"]
            else:
                pending["id"] = self.state["data"]["admin_object_id"]
                pending["revision"] = self.state["data"]["admin_object_revision"]
            self.state_set(step="admin_reason", admin_pending=pending)
            return self.send("Укажите причину сохранения. Она попадёт в журнал аудита.")
        if step == "admin_text":
            require(text, "Введите текст")
            d = self.state["data"]
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "object_text",
                    "id": d["object_id"],
                    "revision": d["object_revision"],
                    "command": d["command"],
                    "text": text,
                },
            )
            return self.send("Укажите причину действия. Она сохранится в журнале аудита.")
        if step == "admin_reason":
            self.require_admin()
            require(3 <= len(text) <= 1000, "Причина должна содержать от 3 до 1000 символов")
            pending = self.state["data"].get("admin_pending") or {}
            operation = pending.get("type")
            result = None
            if operation == "content_create":
                result = self.api(
                    "/admin/content",
                    "POST",
                    {
                        "kind": pending["kind"],
                        "title": pending["title"],
                        "data": pending["data"],
                        "reason": text,
                    },
                )
            elif operation == "content_edit":
                result = self.api(
                    "/admin/content/" + pending["id"],
                    "PATCH",
                    {
                        "revision": pending["revision"],
                        "title": pending["title"],
                        "data": pending["data"],
                        "reason": text,
                    },
                )
            elif operation == "content_action":
                result = self.api(
                    "/admin/content/" + pending["id"] + "/" + pending["command"],
                    "POST",
                    {"revision": pending["revision"], "reason": text},
                )
            elif operation == "content_patch":
                result = self.api(
                    "/admin/content/" + pending["id"],
                    "PATCH",
                    {
                        "revision": pending["revision"],
                        "title": pending["title"],
                        "data": pending["data"],
                        "reason": text,
                    },
                )
            elif operation == "material_render":
                result = self.api(
                    "/admin/materials/" + pending["id"] + "/render-pdf",
                    "POST",
                    {"revision": pending["revision"], "reason": text},
                )
            elif operation == "account_action":
                self.api(
                    "/admin/accounts/" + pending["id"] + "/" + pending["command"],
                    "POST",
                    {"reason": text},
                )
                self.state_set(step=None, admin_pending=None)
                return self.admin_account_card(pending["id"])
            elif operation == "outbox_retry":
                self.api(
                    "/admin/outbox/" + pending["id"] + "/retry",
                    "POST",
                    {"reason": text},
                )
                self.state_set(step=None, admin_pending=None)
                return self.admin_outbox_card(pending["id"])
            elif operation == "subject_edit":
                self.api(
                    "/admin/subjects/" + pending["id"],
                    "POST",
                    {
                        "revision": pending["revision"],
                        **pending["values"],
                        "reason": text,
                    },
                )
                result = {"id": pending["id"], "kind": "object"}
            elif operation == "right_grant":
                granted = self.api(
                    "/admin/rights/grant",
                    "POST",
                    {
                        "subject_id": pending["subject_id"],
                        **pending["values"],
                        "reason": text,
                        "key": pending["key"],
                    },
                )
                result = {"id": granted["id"], "kind": "object"}
            elif operation == "object_action":
                self.api(
                    "/admin/" + pending["route"] + "/" + pending["id"] + "/" + pending["command"],
                    "POST",
                    {
                        "revision": pending["revision"],
                        "reason": text,
                        "text_revision": pending["text_revision"],
                    },
                )
                result = {"id": pending["id"], "kind": "object"}
            elif operation == "object_action_extra":
                self.api(
                    "/admin/" + pending["route"] + "/" + pending["id"] + "/" + pending["command"],
                    "POST",
                    {
                        "revision": pending["revision"],
                        "reason": text,
                        **pending["extra"],
                    },
                )
                result = {"id": pending["id"], "kind": "object"}
            elif operation == "object_text":
                route, command = pending["command"].split("/")
                self.api(
                    "/admin/" + route + "/" + pending["id"] + "/" + command,
                    "POST",
                    {
                        "revision": pending["revision"],
                        "text": pending["text"],
                        "reason": text,
                    },
                )
                result = {"id": pending["id"], "kind": "object"}
            elif operation == "refund":
                refund = self.api(
                    "/admin/orders/" + pending["id"] + "/refund",
                    "POST",
                    {
                        "revision": pending["revision"],
                        "charge": pending["charge"],
                        "amount": pending["amount"],
                        "reason": text,
                    },
                )
                result = {"id": refund["id"], "kind": "object"}
            elif operation == "material_create":
                result = self.api(
                    "/admin/content",
                    "POST",
                    {
                        "kind": "material",
                        "title": pending["title"],
                        "data": pending["data"],
                        "reason": text,
                    },
                )
            else:
                raise Problem("Действие устарело. Откройте /admin заново")
            self.state_set(
                step=None,
                admin_pending=None,
                admin_object_id=None,
                admin_object_revision=None,
                admin_kind=None,
                object_id=None,
                object_revision=None,
                command=None,
            )
            if result.get("kind") == "object":
                return self.admin_card(result["id"])
            return self.admin_content_card(result["id"])
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
            self.state_set(
                step="admin_reason",
                admin_pending={
                    "type": "material_create",
                    "title": self.state["data"]["material_title"],
                    "data": {
                        "owner": "Специалист",
                        "status": "received",
                        "text": text,
                        "source": "Загружено специалистом через Telegram",
                        "rights": "",
                    },
                },
            )
            self.send("Укажите причину сохранения исходника. Она попадёт в журнал аудита.")
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
