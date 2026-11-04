"""Question presentation through public runs and the Telegram transport boundary."""

import copy
import json

import pytest

from marina.bot import Bot
from marina.db import connect
from marina.funnel_bot import show_question
from tests.test_funnel import capture, private_source, scenario, start
from tests.test_identity import app, login
from tests.test_screening import publish


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
def test_all_six_questions_hide_numbering_without_changing_answers_or_score(app, group):
    admin, owner = login(app), login(app, 2)
    content = scenario(admin, group)
    run = start(owner, group)
    sent, _ = capture(app)
    bot = Bot(app, {"id": 2, "first_name": "Fixture"}, 2, "questions")

    for number, value in enumerate("abcabc", 1):
        question = run["data"]["question"]
        heading = f"[Вопрос {number} из 6] "
        assert question["text"].startswith(heading)
        show_question(bot, run)
        endpoint, message = sent[-1]
        assert endpoint == "sendMessage"
        assert message["text"] == question["text"][len(heading) :] + "\n\n" + "\n\n".join(
            f"{letter}. {option['text']}" for letter, option in zip("АБВ", question["options"])
        )
        buttons = [button for row in message["reply_markup"]["inline_keyboard"] for button in row]
        assert [button["text"] for button in buttons] == list("АБВ")
        with connect(app.config["DATABASE"]) as db:
            callbacks = [
                json.loads(
                    db.execute(
                        "SELECT data FROM objects WHERE id=?", (button["callback_data"][2:],)
                    ).fetchone()[0]
                )
                for button in buttons
            ]
        assert callbacks == [
            {
                "action": "funnel_answer",
                "id": run["id"],
                "revision": run["revision"],
                "question": question["id"],
                "value": option["id"],
            }
            for option in question["options"]
        ]
        assert owner.get("/api/funnel-runs/" + run["id"]).json["data"]["question"] == question
        response = owner.post(
            "/api/funnel-runs/" + run["id"] + "/answer",
            json={"revision": run["revision"], "question": question["id"], "value": value},
        )
        assert response.status_code == 200, response.json
        run = response.json

    assert run["status"] == "completed"
    assert run["data"]["result"]["score"] == 6
    assert run["data"]["result"]["zone"] == "yellow"
    with connect(app.config["DATABASE"]) as db:
        stored = json.loads(
            db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()[0]
        )
    assert stored["snapshot"]["method"]["questions"] == content["data"]["method"]["questions"]


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
def test_resume_hides_numbering_from_an_old_run_after_new_content_is_published(app, group):
    admin, owner = login(app), login(app, 2)
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Synthetic privacy policy",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    consent = owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    assert consent.status_code == 200, consent.json
    original = scenario(admin, group)
    run = start(owner, group)
    for number in range(1, 6):
        response = owner.post(
            "/api/funnel-runs/" + run["id"] + "/answer",
            json={"revision": run["revision"], "question": f"q{number}", "value": "a"},
        )
        assert response.status_code == 200, response.json
        run = response.json
    old_question = run["data"]["question"]
    method = copy.deepcopy(original["data"]["method"])
    for question in method["questions"]:
        question["text"] = "Новая редакция вопроса " + question["id"]
    replacement = scenario(admin, group, method=method)
    assert replacement["id"] != original["id"]

    sent, _ = capture(app)
    bot = Bot(app, {"id": 2, "first_name": "Fixture"}, 2, "resume-legacy-question")
    bot.state_set(funnel_run_id=run["id"])
    bot.action("funnel_resume", {})
    assert sent[-1][0] == "sendMessage"
    assert sent[-1][1]["text"].startswith(f"Синтетический вопрос {group}\n\nА. ")
    assert "[Вопрос 6 из 6]" not in sent[-1][1]["text"]
    assert "Новая редакция" not in sent[-1][1]["text"]
    resumed = owner.get("/api/funnel-runs/" + run["id"]).json
    assert resumed["data"]["question"] == old_question
    assert resumed["revision"] == run["revision"]


def test_a_numbering_phrase_inside_the_question_body_is_preserved(app):
    admin, owner = login(app), login(app, 2)
    base = admin.get("/api/admin/funnel-defaults/3-4").json
    method = base["method"]
    body = "Прочитайте фразу [Вопрос 6 из 6] и выберите ответ."
    method["questions"][0]["text"] = body
    scenario(admin, method=method)
    run = start(owner)
    sent, _ = capture(app)
    bot = Bot(app, {"id": 2}, 2, "body-with-brackets")
    show_question(bot, run)
    assert sent[-1][1]["text"].startswith(body + "\n\nА. ")
