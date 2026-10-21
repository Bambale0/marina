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
def test_welcome_funnel_to_test(app, test_available):
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
    assert [[b["text"] for b in row] for row in sent[-1]["reply_markup"]["inline_keyboard"]] == [
        ["пройти тесты"]
    ]
    click("пройти тесты")
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
        publish(admin, "method", method())
    send("/start")
    click("пройти тесты")
    assert sent[-1]["text"] == "Test consent"
    click("Прочитал(а) и принимаю")
    click("Добавить взрослого")
    send("Parent")
    send("1990-01-01")
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
    click("Parent")
    assert "тест" in sent[-1]["text"]
    send("/menu")
    assert "Выберите следующий шаг" in sent[-1]["text"]
    send("/profiles")
    click("Parent")
    assert sent[-1]["text"] == "Профиль выбран."
