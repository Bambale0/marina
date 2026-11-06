"""A single click records each independent choice without repeating the prompt."""

import pytest

from marina import create_app
from marina.bot import Bot, handle_update
from marina.db import connect, transaction
from tests.test_identity import app, login
from tests.test_privacy_documents import privacy


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("legacy", [False, True])
def test_first_marketing_click_records_choice_and_continues_after_restart(app, accepted, legacy):
    privacy(login(app))
    sent = []
    app.config["FILE_TRANSPORT"] = lambda *args: None
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    bot = Bot(app, {"id": 2}, 2, "privacy-start")
    bot.action("profiles", {})

    def click(button, update_id, target):
        handle_update(
            target,
            {
                "update_id": update_id,
                "callback_query": {
                    "id": "privacy-" + str(update_id),
                    "from": {"id": 2},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    consent = sent[-1]["reply_markup"]["inline_keyboard"][0][0]
    click(consent, 21001, app)
    marketing = sent[-1]["reply_markup"]["inline_keyboard"][0 if accepted else 1][0]
    if legacy:
        with transaction(app.config["DATABASE"]) as db:
            db.execute(
                "UPDATE objects SET data=json_set(data,'$.action','consent') WHERE id=?",
                (marketing["callback_data"][2:],),
            )
    restarted = create_app(dict(app.config))
    click(marketing, 21002, restarted)
    assert "Это необязательно" not in sent[-1].get("text", "")
    with connect(app.config["DATABASE"]) as db:
        rows = db.execute(
            "SELECT data FROM objects WHERE kind='marketing_consent' AND owner=?",
            (bot.owner,),
        ).fetchall()
        import json

        assert len(rows) == 1
        assert json.loads(rows[0]["data"])["accepted"] is accepted
