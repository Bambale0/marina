import json

import pytest

from marina.bot import handle_update
from marina.db import connect, transaction, unpack, update
from tests.funnel_fixtures import configure_funnel_source
from tests.test_funnel import scenario
from tests.test_identity import app, login
from tests.test_privacy_documents import privacy


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("group", [None, "3-4", "5-6", "7-9", "10-14"])
@pytest.mark.parametrize("legacy_button", [False, True])
def test_marketing_choice_saves_on_first_telegram_click(
    app, tmp_path, accepted, group, legacy_button
):
    admin, owner = login(app), login(app, 2)
    policy = privacy(admin)
    app.config["ENFORCE_CONSENT"] = True
    if group:
        configure_funnel_source(app, tmp_path)
        scenario(admin, group)
    sent = []

    def transport(method, url, data, headers):
        if url.endswith("/sendMessage"):
            sent.append(data)
        return {"ok": True, "result": {}}

    app.config["HTTP_TRANSPORT"] = transport
    app.config["FILE_TRANSPORT"] = lambda *args: None
    updates = iter(range(100, 120))
    user = {"id": 2, "first_name": "Fixture"}
    chat = {"id": 2, "type": "private"}

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
                "update_id": next(updates),
                "callback_query": {
                    "id": "fixture",
                    "from": user,
                    "data": button["callback_data"],
                    "message": {"chat": chat},
                },
            },
        )

    handle_update(
        app,
        {
            "update_id": next(updates),
            "message": {"from": user, "chat": chat, "text": "/start" if group else "/profiles"},
        },
    )
    if group:
        from marina.funnel_bot import age_buttons

        click(next(label for label, _, data in age_buttons("") if data["age_group"] == group))
    click("Даю согласие на обработку данных")
    assert owner.get("/api/privacy").json["accepted"] is True
    marketing_prompt = sent[-1]["text"]
    with connect(app.config["DATABASE"]) as db:
        for row in sent[-1]["reply_markup"]["inline_keyboard"]:
            for button in row:
                payload = db.execute(
                    "SELECT data FROM objects WHERE id=?", (button["callback_data"][2:],)
                ).fetchone()[0]
                assert json.loads(payload)["action"] == "marketing_choice"
    if legacy_button:
        # Preserve the payload of buttons already sent by the broken version.
        with transaction(app.config["DATABASE"]) as db:
            for row in sent[-1]["reply_markup"]["inline_keyboard"]:
                for button in row:
                    callback = unpack(
                        db.execute(
                            "SELECT * FROM objects WHERE id=?", (button["callback_data"][2:],)
                        ).fetchone()
                    )
                    update(db, callback, app.clock[0], {**callback["data"], "action": "consent"})
    actor = owner.get("/api/me").json["account"]["id"]
    with connect(app.config["DATABASE"]) as db:
        assert not db.execute(
            "SELECT 1 FROM objects WHERE kind='marketing_consent' AND owner=?", (actor,)
        ).fetchone()

    click("Да, получать предложения" if accepted else "Нет, продолжить без рекламы")

    with connect(app.config["DATABASE"]) as db:
        choices = db.execute(
            "SELECT data,status FROM objects WHERE kind='marketing_consent' AND owner=?",
            (actor,),
        ).fetchall()
        assert len(choices) == 1
        assert json.loads(choices[0]["data"])["accepted"] is accepted
        assert json.loads(choices[0]["data"])["policy_id"] == policy["id"]
        assert choices[0]["status"] == ("active" if accepted else "revoked")
        assert (
            db.execute(
                "SELECT count(*) FROM objects WHERE kind='consent' AND owner=?", (actor,)
            ).fetchone()[0]
            == 1
        )
    assert sum(message["text"] == marketing_prompt for message in sent) == 1
    if group:
        assert "Синтетический вопрос " + group in sent[-1]["text"]
    else:
        assert "Добавить ребёнка" in str(sent[-1]["reply_markup"])


@pytest.mark.parametrize("choice", [None, True, False])
def test_start_recovers_missing_marketing_choice_without_repeating_consent(app, tmp_path, choice):
    admin, owner = login(app), login(app, 2)
    policy = privacy(admin)
    configure_funnel_source(app, tmp_path)
    scenario(admin, "3-4")
    assert (
        owner.post(
            "/api/consents",
            json={"policy_id": policy["id"], "accepted": True, "representative_confirmed": True},
        ).status_code
        == 200
    )
    if choice is not None:
        assert (
            owner.post(
                "/api/marketing-consents", json={"policy_id": policy["id"], "accepted": choice}
            ).status_code
            == 200
        )

    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: (
        sent.append(data) if url.endswith("/sendMessage") else None
    ) or {"ok": True, "result": {}}
    app.config["FILE_TRANSPORT"] = lambda *args: None
    user, chat = {"id": 2, "first_name": "Fixture"}, {"id": 2, "type": "private"}

    def start(update_id):
        handle_update(
            app, {"update_id": update_id, "message": {"from": user, "chat": chat, "text": "/start"}}
        )

    start(31001)
    if choice is None:
        assert "Это необязательно" in sent[-1]["text"]
        button = sent[-1]["reply_markup"]["inline_keyboard"][0][0]
        assert button["text"] == "Да, получать предложения"
        handle_update(
            app,
            {
                "update_id": 31002,
                "callback_query": {
                    "id": "marketing-start",
                    "from": user,
                    "data": button["callback_data"],
                    "message": {"chat": chat},
                },
            },
        )
        assert "Это необязательно" not in sent[-1]["text"]
        assert owner.get("/api/privacy").json["marketing_choice"] is True
        start(31003)
    assert "Это необязательно" not in sent[-1]["text"]
    assert "3–4" in str(sent[-1]["reply_markup"])
    assert owner.get("/api/privacy").json["accepted"] is True
