import json

import pytest

from marina.bot import handle_update
from tests.test_identity import app


def test_offer_document_is_complete_published_and_separate_from_consent(app):
    from tests.test_identity import login
    from tests.test_screening import publish

    client, admin = login(app, 2), login(app)
    assert client.get("/api/offer").status_code == 404
    text = "Оферта\n\n" + "Полный текст договора.\n" * 500
    offer = publish(admin, "template", {"type": "offer", "text": text})
    admin.post(
        "/api/admin/content",
        json={
            "kind": "template",
            "title": "Unapproved offer",
            "data": {"type": "offer", "text": "draft"},
            "reason": "New draft must not replace publication",
        },
    )
    assert client.get("/api/offer").json["text"] == text
    assert client.get("/api/offer").json["id"] == offer["id"]
    assert client.get("/api/privacy").json == {"policy": None, "accepted": False}
    files, messages = [], []
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: messages.append(data) or {
        "ok": True,
        "result": {},
    }
    handle_update(
        app,
        {
            "update_id": 8000,
            "message": {
                "from": {"id": 2},
                "chat": {"id": 2, "type": "private"},
                "text": "/offer",
            },
        },
    )
    assert files == [
        (2, "offer.txt", text.encode("utf-8"), "text/plain; charset=utf-8", "sendDocument")
    ]
    assert "Оферта" in messages[-1]["text"]
    # Opening the document from the blocked task keeps the continuation callback.
    from marina.bot import Bot

    from marina.onboarding import AGE_GROUPS

    choice = {
        "age_group": next(iter(AGE_GROUPS)),
        "concern": {"id": "fixture", "text": "Fixture concern"},
    }
    bot = Bot(app, {"id": 2}, 2, 8001)
    bot.action("submit_review_video", choice)
    button = messages[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Оферта"

    def click(button, update_id):
        handle_update(
            app,
            {
                "update_id": update_id,
                "callback_query": {
                    "id": "offer",
                    "from": {"id": 2},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    click(button, 8002)
    assert len(files) == 2 and files[-1][2] == text.encode("utf-8")
    keyboard = messages[-1]["reply_markup"]["inline_keyboard"]
    accept, cancel = keyboard[0][0], keyboard[1][0]
    assert accept["text"] == "✅ Принять оферту"
    assert cancel["text"] == "❌ Отменить"
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Separate consent",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    click(accept, 8003)
    with app.app_context():
        from marina.db import connect

        with connect(app.config["DATABASE"]) as db:
            acceptance = db.execute(
                "SELECT status,data FROM objects WHERE kind='offer_acceptance' AND owner=?",
                (bot.owner,),
            ).fetchone()
            assert acceptance["status"] == "recorded"
            assert json.loads(acceptance["data"])["offer_id"] == offer["id"]
    assert messages[-1]["text"] == "Separate consent"

    app.config["DEV_OFFER_BYPASSES_CONSENT"] = True
    click(accept, 8004)
    assert "Теперь выберите профиль ребёнка" in messages[-1]["text"]

    click(cancel, 8005)
    assert messages[-1]["text"].startswith("Нейроразвитие дома")


def test_polling_profile_and_private_identity(app):
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(
        (url.rsplit("/", 1)[-1], data)
    ) or {"ok": True, "result": {}}

    def message(id, text, user=2):
        handle_update(
            app,
            {
                "update_id": id,
                "message": {
                    "from": {"id": user, "first_name": "Fixture"},
                    "chat": {"id": user, "type": "private"},
                    "text": text,
                },
            },
        )

    message(1, "/start")
    assert "Здравствуйте, Fixture" in sent[-1][1]["text"]
    from tests.test_screening import publish
    from tests.test_identity import login

    admin = login(app)
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Test consent",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    message(2, "/profiles")
    consent = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    handle_update(
        app,
        {
            "update_id": 20,
            "callback_query": {
                "id": "consent",
                "from": {"id": 2, "first_name": "Fixture"},
                "data": consent,
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    add = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    handle_update(
        app,
        {
            "update_id": 3,
            "callback_query": {
                "id": "cb",
                "from": {"id": 2, "first_name": "Fixture"},
                "data": add,
                "message": {"chat": {"id": 2, "type": "private"}},
            },
        },
    )
    message(4, "Parent")
    message(5, "1990-01-01")
    assert "Профиль создан" in sent[-1][1]["text"]
    message(6, "/login")
    assert sent[-1][1]["text"] == "Недостаточно прав"
    message(7, "/admin")
    assert sent[-1][1]["text"] == "Недостаточно прав"
    message(8, "/admin", user=1)
    assert "админка" in sent[-1][1]["text"].lower()
    buttons = [row[0] for row in sent[-1][1]["reply_markup"]["inline_keyboard"]]
    assert [button["text"] for button in buttons] == [
        "Трипваер",
        "Клиенты и доступ",
        "Контент и настройки",
        "Все сущности",
        "Поиск сущности",
        "Очередь",
        "Операции и интеграции",
        "Журнал аудита",
    ]
    assert all("callback_data" in button and "web_app" not in button for button in buttons)
    message(81, "/login", user=1)
    assert sent[-1][1]["text"] == "Mini App не используется. Управление доступно прямо в Telegram."
    assert "callback_data" in sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    message(9, "/queue", user=1)
    assert sent[-1][1]["text"] == "Очередь пуста."


def test_telegram_admin_creates_approves_lists_and_audits_content(app):
    from tests.test_identity import login

    sent, files = [], []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)

    counter = iter(range(9000, 9100))

    def message(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {
                    "from": {"id": 1, "first_name": "Admin"},
                    "chat": {"id": 1, "type": "private"},
                    "text": text,
                },
            },
        )

    def button(title):
        for row in sent[-1]["reply_markup"]["inline_keyboard"]:
            for value in row:
                if value["text"] == title:
                    return value
        raise AssertionError("button not found: " + title)

    def click(value):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "admin-callback",
                    "from": {"id": 1, "first_name": "Admin"},
                    "data": value["callback_data"],
                    "message": {"chat": {"id": 1, "type": "private"}},
                },
            },
        )

    message("/admin")
    content_navigation = button("Контент и настройки")
    click(button("Трипваер"))
    assert "Трипваер" in sent[-1]["text"]
    assert "3–4 года: неполный комплект" in sent[-1]["text"]
    click(content_navigation)
    click(button("Решения"))
    click(button("Создать"))
    message(
        json.dumps(
            {
                "title": "Управляемое решение",
                "data": {"owner": "specialist", "status": "open", "epics": ["E02"]},
            },
            ensure_ascii=False,
        )
    )
    assert "причину" in sent[-1]["text"].lower()
    message("Добавление решения для ручного теста")
    assert "Управляемое решение" in sent[-1]["text"]
    click(button("Скачать JSON"))
    assert files and files[-1][1].startswith("decision-")
    exported = json.loads(files[-1][2])
    assert exported["title"] == "Управляемое решение"
    click(button("Изменить JSON"))
    message(
        json.dumps(
            {
                "title": "Управляемое решение 2",
                "data": {"owner": "specialist", "status": "checked", "epics": ["E02"]},
            },
            ensure_ascii=False,
        )
    )
    message("Уточнение редакции перед проверкой")
    assert "Управляемое решение 2" in sent[-1]["text"]
    click(button("Утвердить"))
    assert "причину" in sent[-1]["text"].lower()
    message("Проверена специалистом перед публикацией")
    assert "Статус: approved" in sent[-1]["text"]
    click(button("Архивировать"))
    assert "причину" in sent[-1]["text"].lower()
    click(content_navigation)
    message("Этот текст не должен подтвердить скрытое действие")

    admin = login(app)
    decisions = admin.get("/api/admin/content?kind=decision&order=desc").json["items"]
    assert decisions[0]["data"]["title"] == "Управляемое решение 2"
    assert decisions[0]["status"] == "approved"
    audit = admin.get("/api/admin/audit?order=desc").json["items"]
    assert audit[0]["reason"] == "Проверена специалистом перед публикацией"

    message("/admin")
    click(button("Все сущности"))
    assert "Решения: 1" in sent[-1]["text"]
    click(button("В админку"))
    click(button("Журнал аудита"))
    assert "Проверена специалистом перед публикацией" in sent[-1]["text"]


def test_telegram_admin_manages_account_access_with_reasons(app):
    from tests.test_identity import login

    owner, admin = login(app, 2), login(app)
    account_id = owner.get("/api/me").json["account"]["id"]
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    counter = iter(range(9200, 9300))

    def message(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {
                    "from": {"id": 1, "first_name": "Admin"},
                    "chat": {"id": 1, "type": "private"},
                    "text": text,
                },
            },
        )

    def click(title):
        value = next(
            value
            for row in sent[-1]["reply_markup"]["inline_keyboard"]
            for value in row
            if value["text"] == title
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "account-callback",
                    "from": {"id": 1, "first_name": "Admin"},
                    "data": value["callback_data"],
                    "message": {"chat": {"id": 1, "type": "private"}},
                },
            },
        )

    message("/admin")
    click("Клиенты и доступ")
    click("Найти клиента")
    message(account_id)
    click("User · активен")
    assert account_id in sent[-1]["text"]
    click("Ограничить вход")
    message("Доступ ограничен по запросу владельца")
    assert owner.get("/api/me").status_code == 403
    click("Восстановить вход")
    message("Владелец подтвердил восстановление")
    assert owner.get("/api/me").status_code == 200
    click("Отозвать сессии")
    message("Плановая смена активных сессий")
    assert owner.get("/api/me").status_code == 401
    reasons = [item["reason"] for item in admin.get("/api/admin/audit").json["items"]]
    assert "Доступ ограничен по запросу владельца" in reasons
    assert "Владелец подтвердил восстановление" in reasons
    assert "Плановая смена активных сессий" in reasons


def test_telegram_admin_renders_and_uploads_material_assets(app):
    from tests.test_identity import login

    admin = login(app)
    rendered = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Текстовый PDF",
            "data": {
                "owner": "specialist",
                "status": "ready",
                "source": "Owner text",
                "rights": "Owned",
                "text": "Базовый текст для ручного теста",
                "filename": "manual-test.pdf",
            },
            "reason": "Подготовка исходника",
        },
    ).json
    uploaded = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Загруженный файл",
            "data": {
                "owner": "specialist",
                "status": "received",
                "source": "Owner file",
                "rights": "Owned",
            },
            "reason": "Подготовка загрузки",
        },
    ).json
    sent, files = [], []

    def transport(method, url, data, headers):
        if url.endswith("/getFile"):
            return {"ok": True, "result": {"file_path": "documents/material.txt"}}
        sent.append(data)
        return {"ok": True, "result": {}}

    app.config["HTTP_TRANSPORT"] = transport
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    app.config["TELEGRAM_FILE_TRANSPORT"] = lambda path, limit: b"Uploaded owner material"
    counter = iter(range(9400, 9500))

    def callback(action, data):
        from marina.bot import Bot

        Bot(app, {"id": 1, "first_name": "Admin"}, 1, next(counter)).action(action, data)

    def message(text=None, document=None):
        value = {
            "from": {"id": 1, "first_name": "Admin"},
            "chat": {"id": 1, "type": "private"},
        }
        if text is not None:
            value["text"] = text
        if document is not None:
            value["document"] = document
        handle_update(app, {"update_id": next(counter), "message": value})

    callback("admin_material_render", {"id": rendered["id"]})
    message("Сформировать PDF из проверенного текста")
    current = admin.get("/api/admin/content/" + rendered["id"]).json
    assert current["data"]["asset_id"]
    assert admin.get("/api/admin/materials/" + rendered["id"] + "/pdf").data.startswith(b"%PDF")
    callback("admin_material_pdf", {"id": rendered["id"]})
    assert files[-1][1] == "manual-test.pdf"
    assert files[-1][2].startswith(b"%PDF")
    callback("admin_content_action", {"id": rendered["id"], "command": "approve"})
    message("Материал проверен специалистом")
    labels = [value["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for value in row]
    assert "Скачать PDF" in labels
    assert "Опубликовать" in labels
    callback("admin_content_action", {"id": rendered["id"], "command": "publish"})
    message("Публикация проверенного материала")
    assert admin.get("/api/admin/content/" + rendered["id"]).json["status"] == "published"

    callback("admin_asset_start", {"id": uploaded["id"]})
    message("Загрузить принадлежащий владельцу текстовый файл")
    message(
        document={
            "file_id": "owner-material",
            "file_size": 23,
            "file_name": "material.txt",
            "mime_type": "text/plain",
        }
    )
    stored = admin.get("/api/admin/content/" + uploaded["id"]).json
    assert stored["data"]["asset_id"]
    reasons = [item["reason"] for item in admin.get("/api/admin/audit").json["items"]]
    assert "Сформировать PDF из проверенного текста" in reasons
    assert "Загрузить принадлежащий владельцу текстовый файл" in reasons


def test_telegram_admin_paginates_outbox_and_opens_older_event(app):
    from marina.bot import Bot
    from marina.db import emit, transaction
    from tests.test_identity import login

    owner, admin = login(app, 2), login(app)
    owner_id = owner.get("/api/me").json["account"]["id"]
    event_ids = []
    with transaction(app.config["DATABASE"]) as db:
        for index in range(35):
            event = "test.event." + str(index)
            emit(
                db,
                owner_id,
                "object-" + str(index),
                event,
                app.clock[0] + index,
            )
            event_ids.append(
                db.execute("SELECT id FROM outbox WHERE event=?", (event,)).fetchone()[0]
            )

    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    bot = Bot(app, {"id": 1, "first_name": "Admin"}, 1, 9550)
    bot.admin_operations()
    labels = [value["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for value in row]
    assert "Далее" in labels
    assert "показаны 1–30" in sent[-1]["text"]

    bot.action("admin_operations", {"offset": 30})
    labels = [value["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for value in row]
    assert "Назад" in labels
    assert "Далее" not in labels
    assert "показаны 31–35" in sent[-1]["text"]

    assert owner.get("/api/admin/outbox/" + event_ids[0]).status_code == 403
    assert admin.get("/api/admin/outbox/" + event_ids[0]).json["id"] == event_ids[0]
    bot.admin_outbox_card(event_ids[0])
    assert event_ids[0] in sent[-1]["text"]


def test_telegram_admin_searches_related_objects_and_revokes_right(app):
    from marina.db import insert, transaction
    from tests.test_identity import login

    owner, admin = login(app, 2), login(app)
    owner_id = owner.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        order = insert(
            db, "order", {"title": "Связанный заказ"}, app.clock[0], owner_id, status="paid"
        )
        right = insert(
            db,
            "right",
            {"service": "guide", "order_id": order["id"]},
            app.clock[0],
            owner_id,
            status="active",
        )
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    counter = iter(range(9600, 9700))

    def message(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {
                    "from": {"id": 1, "first_name": "Admin"},
                    "chat": {"id": 1, "type": "private"},
                    "text": text,
                },
            },
        )

    def click(title):
        value = next(
            value
            for row in sent[-1]["reply_markup"]["inline_keyboard"]
            for value in row
            if value["text"] == title
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "object-callback",
                    "from": {"id": 1, "first_name": "Admin"},
                    "data": value["callback_data"],
                    "message": {"chat": {"id": 1, "type": "private"}},
                },
            },
        )

    message("/admin")
    click("Поиск сущности")
    message(right["id"])
    labels = [value["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for value in row]
    assert "Отозвать право" in labels
    assert "Аккаунт владельца" in labels
    assert "Открыть order_id" in labels
    click("Отозвать право")
    message("Компенсационное право выдано ошибочно")
    stored = admin.get("/api/admin/objects/" + right["id"]).json["object"]
    assert stored["status"] == "revoked"
    assert (
        admin.get("/api/admin/audit?object_id=" + right["id"]).json["items"][-1]["reason"]
        == "Компенсационное право выдано ошибочно"
    )


@pytest.mark.parametrize(
    "group, count", [("3–4 года", 5), ("5–6 лет", 5), ("7–8 лет", 7), ("9–10 лет", 6)]
)
def test_welcome_funnel_to_video_review(app, tmp_path, group, count):
    from datetime import datetime, timezone
    from tests.test_identity import login
    from tests.test_screening import publish
    from tests.test_crm import make_video

    app.config["ENFORCE_CONSENT"] = True
    sent = []

    def transport(method, url, data, headers):
        if url.endswith("/getFile"):
            return {"ok": True, "result": {"file_path": "videos/fixture.mp4"}}
        sent.append(data)
        return {"ok": True, "result": {}}

    app.config["HTTP_TRANSPORT"] = transport
    counter = iter(range(100, 300))

    def send(text=None, video=None):
        message = {"from": {"id": 2}, "chat": {"id": 2, "type": "private"}}
        if text is not None:
            message["text"] = text
        if video is not None:
            message["video"] = video
        handle_update(app, {"update_id": next(counter), "message": message})

    def click(title):
        b = next(
            b
            for row in sent[-1]["reply_markup"]["inline_keyboard"]
            for b in row
            if b["text"] == title
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "fixture",
                    "from": {"id": 2},
                    "data": b["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    def choose_task(number):
        send("/video")
        click("пройти тесты")
        click(group)
        click("1")
        click("Продолжить")
        click("Вариант " + str(number))

    send("/video")
    assert sent[-1]["text"].startswith("Привет")
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["style"] == "success"
    click("пройти тесты")
    click(group)
    rows = sent[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == {5: [3, 2, 1, 1], 6: [3, 3, 1, 1], 7: [3, 2, 2, 1, 1]}[
        count
    ]
    assert [b["text"] for row in rows[:-2] for b in row] == [str(i) for i in range(1, count + 1)]
    click("1")
    click("Продолжить")
    click("Вариант 1")
    assert "Что заметить" in sent[-1]["text"]
    click("Прислать видео специалисту")
    assert "готовит" in sent[-1]["text"]
    admin = login(app)
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Test consent",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    # The blocked screen retains the selected task: retry after policy publication.
    click("Прислать видео специалисту")
    assert sent[-1]["text"] == "Test consent"
    click("Прочитал(а) и принимаю")
    click("Добавить ребёнка")
    send("Child")
    send("1990-01-01")
    assert "не соответствует" in sent[-1]["text"]
    today = datetime.fromtimestamp(app.clock[0], timezone.utc).date()
    age = {"3–4 года": 3, "5–6 лет": 5, "7–8 лет": 7, "9–10 лет": 9}[group]
    send(today.replace(year=today.year - age).isoformat())
    assert "Пришлите видео" in sent[-1]["text"]
    send("Only text")
    assert "Отправьте видео" in sent[-1]["text"]
    raw = make_video(tmp_path)
    app.config["TELEGRAM_FILE_TRANSPORT"] = lambda path, limit: raw
    send(video={"file_id": "fixture", "file_size": len(raw), "mime_type": "video/mp4"})
    assert "Видео получено" in sent[-1]["text"]
    leads = admin.get("/api/admin/crm").json["items"]
    assert len(leads) == 1 and leads[0]["status"] == "video_received"
    assert leads[0]["data"]["activity"]["id"] == "1"
    assert admin.get("/api/media/" + leads[0]["data"]["video_id"]).data == raw
    choose_task(2)
    click("Прислать видео специалисту")
    click("Child")
    assert "Пришлите видео" in sent[-1]["text"]
    send("/menu")
    send("/profiles")
    click("Child")
    assert sent[-1]["text"] == "Профиль выбран."
    send("/video")
    click("пройти тесты")
    click(group)
    click("добавить свое")
    send("Своя проблема")
    assert "Ваш выбор: Своя проблема" in sent[-1]["text"]
    click("Child")
    assert "Пришлите видео" in sent[-1]["text"]
