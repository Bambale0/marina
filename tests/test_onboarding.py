from marina.bot import handle_update
from marina.onboarding import QUESTIONS
from tests.test_identity import app, login
from tests.test_screening import publish


def test_age_screen_uses_published_admin_copy_and_keeps_old_buttons(app):
    admin, client = login(app), login(app, 2)
    assert client.get("/api/admin/onboarding-defaults/3-4").status_code == 403
    defaults = admin.get("/api/admin/onboarding-defaults/3-4").json
    assert [q["text"] for q in defaults["concerns"]] == QUESTIONS["3-4"]
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: sent.append(data) or {
        "ok": True,
        "result": {},
    }
    counter = iter(range(1, 100))

    def message(text):
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

    def open_screen():
        message("/start")
        click("пройти тесты")
        click("3–4 года")

    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "onboarding",
            "title": "Edited screen",
            "data": {**defaults, "concerns": [{"id": "speech", "text": "Новая формулировка?"}]},
            "reason": "Owner editing introduction",
        },
    ).json
    open_screen()
    assert "1. Ребёнок мало говорит?" in sent[-1]["text"]
    assert "Новая формулировка?" not in sent[-1]["text"]
    for action in ("approve", "publish"):
        draft = admin.post(
            f"/api/admin/content/{draft['id']}/{action}",
            json={"revision": draft["revision"], "reason": "Owner approved introduction"},
        ).json
    open_screen()
    assert "1. Новая формулировка?" in sent[-1]["text"]
    assert len(sent[-1]["reply_markup"]["inline_keyboard"]) == 2
    click("Изменить возраст")
    click("9–10 лет")
    assert "6. Сильно переживает из-за учёбы и ошибок?" in sent[-1]["text"]
    assert "Новая формулировка?" not in sent[-1]["text"]
    # A different age has independent content; copy retains immutable history.
    copy = admin.post(
        f"/api/admin/content/{draft['id']}/copy",
        json={"revision": draft["revision"], "reason": "New introduction revision"},
    ).json
    assert copy["data"]["previous_version"] == draft["id"]
    assert admin.get(f"/api/admin/objects/{draft['id']}/versions").json["items"]


def test_screen_validation_rejects_duplicate_ids_and_empty_text(app):
    admin = login(app)
    for concerns in (
        [{"id": "x", "text": ""}],
        [{"id": "x", "text": "A"}, {"id": "x", "text": "B"}],
    ):
        obj = admin.post(
            "/api/admin/content",
            json={
                "kind": "onboarding",
                "title": "Invalid",
                "data": {"age_group": "3-4", "concerns": concerns},
                "reason": "Validation fixture",
            },
        ).json
        assert (
            admin.post(
                f"/api/admin/content/{obj['id']}/approve",
                json={"revision": obj["revision"], "reason": "Validation fixture"},
            ).status_code
            == 422
        )
