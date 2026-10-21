import pytest

from marina.bot import handle_update
from tests.test_identity import app


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
    assert "Привет" in sent[-1][1]["text"]
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
    code = sent[-1][1]["text"].split("\n")[-1]
    c = app.test_client()
    assert c.post("/api/auth/code", json={"code": code}).status_code == 200
    assert c.post("/api/auth/code", json={"code": code}).status_code == 401
    message(7, "/admin")
    assert "права" in sent[-1][1]["text"]


@pytest.mark.parametrize("test_available", [True, False])
@pytest.mark.parametrize(
    "group, count", [("3–4 года", 5), ("5–6 лет", 5), ("7–8 лет", 7), ("9–10 лет", 6)]
)
def test_welcome_funnel_to_test(app, test_available, group, count):
    from tests.test_identity import login
    from tests.test_screening import method, publish

    app.config["ENFORCE_CONSENT"] = True
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    counter = iter(range(100, 200))

    def send(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {"from": {"id": 2}, "chat": {"id": 2, "type": "private"}, "text": text},
            },
        )

    def click(title):
        button = next(
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
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    send("/start")
    assert "Привет" in sent[-1]["text"]
    assert all(
        b["style"] == "success" for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row
    )
    assert [[b["text"] for b in row] for row in sent[-1]["reply_markup"]["inline_keyboard"]] == [
        ["пройти тесты"]
    ]
    click("пройти тесты")
    assert sent[-1]["text"] == "Сколько лет Вашему ребёнку?"
    assert len(sent[-1]["reply_markup"]["inline_keyboard"]) == 4
    click(group)
    assert "С какой проблемой Вы обращаетесь?" in sent[-1]["text"]
    rows = sent[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == {
        5: [3, 2, 1, 1],
        6: [3, 3, 1, 1],
        7: [3, 2, 2, 1, 1],
    }[count]
    assert [b["text"] for row in rows[:-2] for b in row] == [str(i) for i in range(1, count + 1)]
    assert rows[-2][0]["text"] == "добавить свое"
    assert rows[-1][0]["text"] == "Изменить возраст"
    assert f"{count}. " in sent[-1]["text"]
    click("1")
    click("Продолжить")
    assert "Выберите одно задание" in sent[-1]["text"]
    click("Вариант 1")
    assert "Что заметить" in sent[-1]["text"]
    click("Записать наблюдение")
    assert "готовит" in sent[-1]["text"]
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
    if test_available:
        fixture = method()
        fixture.update(audience="child", min_months=36, max_months=131)
        publish(admin, "method", fixture)
    send("/start")
    click("пройти тесты")
    click(group)
    click("1")
    click("Продолжить")
    click("Вариант 1")
    click("Записать наблюдение")
    assert sent[-1]["text"] == "Test consent"
    owner_id = login(app, 2).get("/api/me").json["account"]["id"]
    assert admin.get("/api/admin/accounts/" + owner_id).json["introduction"] is None
    click("Прочитал(а) и принимаю")
    assert "Что получилось самостоятельно" in sent[-1]["text"]
    assert "В какой момент понадобилась помощь" in sent[-1]["text"]
    send(" ")
    assert "от 1 до 3000" in sent[-1]["text"]
    send("x" * 3001)
    assert "от 1 до 3000" in sent[-1]["text"]
    observation = "Начал сам. Понадобилось повторение. После повторения закончил."
    send(observation)
    assert "наблюдение сохранено" in sent[-1]["text"]
    introduction = admin.get("/api/admin/accounts/" + owner_id).json["introduction"]
    assert introduction["concern"]["id"] == "1"
    assert introduction["concern"]["selected_activity"]["id"] == "1"
    assert introduction["concern"]["observation"] == observation
    assert (
        introduction["age_group"]
        == {"3–4 года": "3-4", "5–6 лет": "5-6", "7–8 лет": "7-8", "9–10 лет": "9-10"}[group]
    )
    click("Добавить ребёнка")
    send("Child")
    send("1990-01-01")
    assert "не соответствует" in sent[-1]["text"]
    from datetime import datetime, timezone

    today = datetime.fromtimestamp(app.clock[0], timezone.utc).date()
    age = {"3–4 года": 3, "5–6 лет": 5, "7–8 лет": 7, "9–10 лет": 9}[group]
    send(today.replace(year=today.year - age).isoformat())
    assert "тест" in sent[-1]["text"]
    if not test_available:
        assert "Подходящий тест пока готовится" in sent[-1]["text"]
        assert "Профиль сохранён" in sent[-1]["text"]
        return
    click("Test fixture only")
    assert sent[-1]["text"] == "Question"
    click("A")
    assert "Low" in sent[-1]["text"]
    send("/start")
    click("пройти тесты")
    click(group)
    click("1")
    click("Продолжить")
    click("Вариант 2")
    click("Записать наблюдение")
    send("Задание получилось с подсказкой.")
    click("Child")
    assert "тест" in sent[-1]["text"]
    send("/menu")
    assert "Выберите следующий шаг" in sent[-1]["text"]
    send("/profiles")
    click("Child")
    assert sent[-1]["text"] == "Профиль выбран."
    send("/start")
    click("пройти тесты")
    click(group)
    click("добавить свое")
    assert "Напишите своими словами" in sent[-1]["text"]
    send(" ")
    assert "от 1 до 1000" in sent[-1]["text"]
    send("x" * 1001)
    assert "от 1 до 1000" in sent[-1]["text"]
    send("Мой собственный вариант проблемы")
    assert "Мой собственный вариант проблемы" in sent[-1]["text"]
    introduction = admin.get("/api/admin/accounts/" + owner_id).json["introduction"]
    assert introduction["concern"] == {"id": "custom", "text": "Мой собственный вариант проблемы"}
