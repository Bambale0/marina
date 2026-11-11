"""Author button copy and callback routing, with transport replaced at the boundary."""

import copy
import json
from urllib.parse import urlparse, parse_qs
import pytest
from marina import Problem
from marina.bot import Bot
from marina.db import connect, transaction
from marina.funnel import notify_funnel
from marina.consultation_bot import show_quote
from tests.test_consultation_remainder import app, journey_sale, route_setup, SOURCE
from tests.test_consultation_commerce import CASES, quote
from tests.test_consultation_bot import click

EXPECTED = {
    "3-4": (
        "📋 Что входит в диагностику 3–4 лет?",
        "📅 Забронировать окно со скидкой {price}",
        "❌ Назад",
    ),
    "5-6": (
        "📋 Что входит в диагностику готовности к школе?",
        "📅 Зафиксировать скидку и выбрать время",
        "❌ Назад к описанию",
    ),
    "7-9": (
        "📋 Что мы смотрим на диагностике школьника?",
        "📅 Забронировать окно со скидкой {price}",
        "❌ Назад к описанию",
    ),
    "10-14": (
        "📋 Как проходит консультация с подростком?",
        "📅 Зафиксировать спеццену {price}",
        "❌ Назад к описанию",
    ),
}


def labels(message):
    return [b for row in message.get("reply_markup", {}).get("inline_keyboard", []) for b in row]


@pytest.mark.parametrize("group,base,promo", CASES)
@pytest.mark.parametrize("coupon", [False, True])
def test_description_checkout_and_back_use_author_labels_and_actual_price(
    app, group, base, promo, coupon
):
    _, owner, run, booking, sent, _, _ = journey_sale(app, group, base, promo, coupon)
    price = f"{(promo if coupon else base // 100):,}".replace(",", " ") + " ₽"
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "author-buttons")
    bot.action("funnel_consultation", {"id": run["id"]})
    text = labels(sent[-1][1])[0]["text"]
    assert text == (
        EXPECTED[group][1].format(price=price) if coupon else "📅 Записаться на диагностику"
    )
    q = quote(owner, booking)
    assert q.status_code == 201, q.json
    show_quote(bot, booking, q.json)
    buttons = labels(sent[-1][1])
    assert buttons[0]["text"] == "💳 Оплатить диагностику " + price
    assert buttons[1]["text"] == EXPECTED[group][2]
    if not coupon:
        assert "скид" not in text.lower()


@pytest.mark.parametrize("group,base,promo", CASES)
@pytest.mark.parametrize("hours", [24, 48, 72])
def test_followup_support_goes_through_author_reply_before_personal_chat(
    app, group, base, promo, hours
):
    _, owner, run, _, sent, _, j = journey_sale(app, group, base, promo, True, book=False)
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    app.clock[0] = current["data"]["guide_delivered_at"] + hours * 3600
    notify_funnel(app, run["owner"], run["id"], "funnel.followup." + str(hours))
    buttons = labels(sent[-1][1])
    support = next(b for b in buttons if b["text"].startswith("💬"))
    assert "callback_data" in support and "url" not in support
    with connect(app.config["DATABASE"]) as db:
        action = json.loads(
            db.execute(
                "SELECT data FROM objects WHERE id=?", (support["callback_data"][2:],)
            ).fetchone()[0]
        )
    assert action == {"action": "funnel_contact", "id": run["id"]}
    before = len(sent)
    click(app, support, 90000 + hours)
    reply = next(
        d for method, d in sent[before:] if method == "sendMessage" and "reply_markup" in d
    )
    assert reply["text"] == j["contact_text"].replace("{name}", "Buyer")
    open_button = labels(reply)[0]
    assert open_button["text"] == "👩‍⚕️ Написать Марине в личные сообщения"
    assert urlparse(open_button["url"]).hostname == "t.me" and parse_qs(
        urlparse(open_button["url"]).query
    ).get("text")


def test_age_information_and_outside_contact_labels_come_from_author_source(app):
    config = json.loads(SOURCE.read_text())
    for group, expected in EXPECTED.items():
        assert config["ages"][group]["buttons"]["info"] == expected[0]
    _, _, _ = route_setup(app)
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": 1},
    }
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "outside-author")
    bot.action("outside_route", {"route": "adult"})
    assert "💬 Задать вопрос в личные сообщения" in [b["text"] for b in labels(calls[-1])]
    bot.action("outside_route", {"route": "baby_0_2"})
    assert labels(calls[-1])[0]["text"] == "📹 Заказать видеоразбор малыша (1 900 ₽)"


def test_outside_video_order_button_preserves_booking_action(app):
    route_setup(app)
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": len(calls)},
    }
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "outside-booking")
    bot.action("outside_route", {"route": "baby_0_2"})
    order = next(
        message
        for message in reversed(calls)
        if labels(message)
        and labels(message)[0]["text"] == "📹 Заказать видеоразбор малыша (1 900 ₽)"
    )

    click(app, labels(order)[0], 99001)

    assert any("Сначала оплатите видеоразбор" in d.get("text", "") for d in calls)
    assert any(
        "Оплатить видеоразбор за 1 900 ₽" in button.get("text", "")
        for message in calls
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
    )
    assert not any(d.get("text") == "Выберите действие" for d in calls)
    with connect(app.config["DATABASE"]) as db:
        lead = db.execute(
            "SELECT data FROM objects WHERE owner=? AND kind='consultation_lead'",
            (bot.owner,),
        ).fetchone()
        task = db.execute(
            "SELECT data FROM objects WHERE owner=? AND kind='video_task'",
            (bot.owner,),
        ).fetchone()
    assert json.loads(lead[0])["action"] == "booking"
    assert json.loads(task[0])["paid_at"] is None
    assert task


@pytest.mark.parametrize(
    "template", ["💳 Оплатить {amount}", "💳 Оплатить 3900 ₽", "💳 Оплатить {price!r}"]
)
def test_publication_rejects_unknown_or_missing_dynamic_price(app, template):
    from marina.consultation_journey import validate_journey

    j = copy.deepcopy(json.loads(SOURCE.read_text())["ages"]["3-4"])
    j["buttons"]["pay"] = template
    with connect(app.config["DATABASE"]) as db:
        with pytest.raises(Problem):
            validate_journey(db, j)


def test_old_run_gets_published_button_copy_without_rewriting_its_snapshot(app):
    _, owner, _, _, _, _, _ = journey_sale(app, book=False)
    from tests.test_funnel import start, finish

    run = finish(owner, start(owner, "3-4", key="legacy-red"), ["c"] * 6)
    with transaction(app.config["DATABASE"]) as db:
        row = db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()
        d = json.loads(row[0])
        d["snapshot"]["consultation_journey"].pop("buttons", None)
        old = json.dumps(d)
        db.execute("UPDATE objects SET data=? WHERE id=?", (old, run["id"]))
    details = owner.get("/api/funnel-runs/" + run["id"] + "/consultation-journey")
    assert details.status_code == 200
    assert details.json["journey"]["buttons"]["info"] == EXPECTED["3-4"][0]
    resumed = owner.get("/api/funnel-runs/" + run["id"]).json
    assert resumed["data"]["result_buttons"][0][0] == EXPECTED["3-4"][0]
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()[0] == old


def test_followup_dynamic_price_is_rendered_from_the_reserved_snapshot(app):
    _, owner, run, _, sent, _, _ = journey_sale(app, coupon=True, book=False)
    with transaction(app.config["DATABASE"]) as db:
        row = db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()
        d = json.loads(row[0])
        d["snapshot"]["followups"][1]["button_label"] = "⚡️ Зафиксировать окно за {price}"
        db.execute("UPDATE objects SET data=? WHERE id=?", (json.dumps(d), run["id"]))
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    app.clock[0] = current["data"]["guide_delivered_at"] + 48 * 3600
    notify_funnel(app, run["owner"], run["id"], "funnel.followup.48")
    assert labels(sent[-1][1])[0]["text"] == "⚡️ Зафиксировать окно за 3 900 ₽"


@pytest.mark.parametrize("coupon", [False, True])
def test_ready_invoice_and_repeat_link_keep_author_caption(app, coupon):
    from tests.test_consultation_bot import transport_capture, messages
    from tests.test_funnel import drain
    from marina.funnel_bot import show_checkout

    _, owner, _, booking, _, _, _ = journey_sale(app, "10-14", 700000, 4500, coupon)
    q = quote(owner, booking).json
    order = owner.post("/api/orders", json={"quote_id": q["id"], "key": "author-invoice"}).json
    calls = transport_capture(app)
    attempt = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    )
    assert attempt.status_code < 400, attempt.json
    drain(app)
    expected = "💳 Оплатить диагностику " + ("4 500 ₽" if coupon else "7 000 ₽")
    ready = [
        b
        for m in messages(calls)
        for b in labels(m)
        if b.get("url") == "https://app.lava.top/consultation"
    ]
    assert ready and ready[-1]["text"] == expected
    repeated = owner.post(
        "/api/orders/" + order["id"] + "/checkout", json={"email": "buyer@example.test"}
    ).json
    bot = Bot(app, {"id": 2, "first_name": "Buyer"}, 2, "ready-repeat")
    show_checkout(bot, repeated)
    assert labels(messages(calls)[-1])[0]["text"] == expected


def test_pending_legacy_followup_uses_new_labels_without_mutating_saved_terms(app):
    _, owner, run, _, sent, _, j = journey_sale(app, coupon=True, book=False)
    with transaction(app.config["DATABASE"]) as db:
        d = json.loads(
            db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()[0]
        )
        d["snapshot"]["consultation_journey"].pop("buttons")
        d["snapshot"]["followups"][0].update(
            button_label="Старое общее название", support_label="Старый вопрос"
        )
        old = json.dumps(d)
        db.execute("UPDATE objects SET data=? WHERE id=?", (old, run["id"]))
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    app.clock[0] = current["data"]["guide_delivered_at"] + 24 * 3600
    notify_funnel(app, run["owner"], run["id"], "funnel.followup.24")
    buttons = labels(sent[-1][1])
    assert buttons[0]["text"] == j["buttons"]["info"]
    assert buttons[1]["text"] == j["buttons"]["support"]
    with connect(app.config["DATABASE"]) as db:
        saved = json.loads(
            db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()[0]
        )
    assert saved.pop("followup_sent_at") == {"24": app.clock[0]}
    assert saved.pop("followup_message_ids")["24"] > 0
    assert saved == json.loads(old)
