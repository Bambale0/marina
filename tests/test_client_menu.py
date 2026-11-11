"""Customer-facing Telegram service menu regression checks."""

from marina.bot import Bot, MENU
from marina.db import connect, insert, transaction
from tests.test_identity import app, login


def capture(app):
    messages = []

    def transport(method, url, data, headers):
        if url.endswith("/sendMessage"):
            messages.append(data)
        return {"ok": True, "result": {"message_id": len(messages)}}

    app.config["HTTP_TRANSPORT"] = transport
    return messages


def buttons(message):
    return [
        button for row in message["reply_markup"]["inline_keyboard"] for button in row
    ]


def test_menu_has_only_four_customer_actions(app):
    messages = capture(app)
    bot = Bot(app, {"id": 2, "first_name": "Parent"}, 2, "menu-four")
    bot.action("menu", {})
    assert [button["text"] for button in buttons(messages[-1])] == [
        "👥 Вступить в группу",
        "💬 Связаться со специалистом",
        "📅 Записаться на онлайн-занятие",
        "💳 Мой кошелёк",
    ]
    assert len(MENU) == 4


def test_group_and_contact_links_come_from_published_settings(app):
    messages = capture(app)
    with transaction(app.config["DATABASE"]) as db:
        insert(
            db,
            "policy",
            {
                "type": "operations",
                "group_url": "https://t.me/example_parent_group",
                "specialist_contact_url": "https://t.me/example_specialist",
            },
            app.clock[0],
            status="published",
        )
    bot = Bot(app, {"id": 2}, 2, "configured-links")
    bot.action("join_group", {})
    assert any(
        button.get("url") == "https://t.me/example_parent_group"
        for button in buttons(messages[-1])
    )
    bot.action("contact_specialist", {})
    assert any(
        button.get("url") == "https://t.me/example_specialist"
        for button in buttons(messages[-1])
    )


def test_unconfigured_links_do_not_invent_destinations(app):
    messages = capture(app)
    bot = Bot(app, {"id": 2}, 2, "missing-links")
    for action in ("join_group", "contact_specialist"):
        bot.action(action, {})
        assert "не настроен" in messages[-1]["text"] or "не опубликован" in messages[-1]["text"]
        assert not any("url" in button for button in buttons(messages[-1]))


def test_online_lesson_uses_owned_profile_and_existing_booking(app):
    messages = capture(app)
    client = login(app, 2)
    profile = client.post(
        "/api/subjects",
        json={"kind": "child", "name": "Test child", "birth_date": "2020-01-01"},
    )
    assert profile.status_code == 201, profile.json
    bot = Bot(app, {"id": 2}, 2, "online-booking")
    bot.state_set(subject_id=profile.json["id"])
    bot.action("online_lesson", {})
    assert "онлайн-занят" in messages[-1]["text"].lower()
    bot.message({"text": "Развитие речи"})
    bot.message({"text": "Пятница, 18:00 МСК"})
    with connect(app.config["DATABASE"]) as db:
        item = db.execute(
            "SELECT data FROM objects WHERE kind='booking' AND owner=?",
            (bot.owner,),
        ).fetchone()
    assert item is not None
    import json

    data = json.loads(item["data"])
    assert data["topic"] == "Онлайн-занятие: Развитие речи"
    assert data["preferences"] == "Пятница, 18:00 МСК"


def test_wallet_is_read_only_until_payment_ledger_is_live(app):
    messages = capture(app)
    bot = Bot(app, {"id": 2}, 2, "wallet-read-only")
    bot.action("wallet", {})
    assert "не подключ" in messages[-1]["text"].lower()
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='order'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_previous_features_remain_reachable_via_services_command(app):
    messages = capture(app)
    bot = Bot(app, {"id": 2}, 2, "legacy-menu")
    bot.message({"text": "/services"})
    names = [button["text"] for button in buttons(messages[-1])]
    assert "Пройти скрининг" in names
    assert "Задание с видео" in names
    assert "Мои обращения" in names
