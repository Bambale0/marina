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
    assert "Выберите" in sent[-1][1]["text"]
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
