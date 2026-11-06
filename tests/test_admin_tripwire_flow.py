"""Admin previews repeat the sales journey; payment is bypassed only on purchase."""

import pytest

from marina.bot import handle_update
from marina.db import connect, transaction, unpack, update
from tests.test_identity import app
from tests.test_funnel import setup_sale, start, finish, drain
from tests.test_screening import publish
from tests.funnel_fixtures import configure_funnel_source


def click(app, button, update_id):
    handle_update(
        app,
        {
            "update_id": update_id,
            "callback_query": {
                "id": "admin-purchase-" + str(update_id),
                "from": {"id": 1, "first_name": "Admin"},
                "data": button["callback_data"],
                "message": {"chat": {"id": 1, "type": "private"}},
            },
        },
    )


def prepare(app, tmp_path, group="3-4"):
    configure_funnel_source(app, tmp_path)
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture",
        PUBLIC_URL="https://fixture.test",
    )
    admin, client, _, _, _, sent, files = setup_sale(
        app, group=group, currency="RUB", lava_offer_id="fixture-offer", web_sales_approved=True
    )
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Synthetic consent",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    assert (
        admin.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True}).status_code
        == 200
    )
    upstream = app.config["HTTP_TRANSPORT"]

    def telegram_only(method, url, data, headers):
        assert url.startswith("https://api.telegram.org/"), (
            "Admin must not contact payment provider"
        )
        return upstream(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = telegram_only
    return admin, client, sent, files


def complete_and_offer(app, admin, group, key, sent):
    marker = len(sent)
    run = finish(admin, start(admin, group, key=key), ["b"] * 6)
    assert run["data"]["result"]["zone"] == "yellow"
    assert run["data"]["result_buttons"] == []
    drain(app)
    app.clock[0] += 30
    drain(app)
    assert "reply_markup" not in sent[-1][1]
    app.clock[0] += 180
    drain(app)
    messages = [data for _, data in sent[marker:] if data.get("chat_id") == 1]
    offers = [
        data
        for data in messages
        if any(
            b.get("text", "").startswith("💳 Забрать руководство за")
            for row in data.get("reply_markup", {}).get("inline_keyboard", [])
            for b in row
        )
    ]
    assert len(offers) == 1
    assert not any("Открыть руководство" in str(data) for data in messages)
    return run, offers[0]["reply_markup"]["inline_keyboard"][0][0]


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
def test_admin_repeated_screening_keeps_offer_then_click_delivers_once(app, tmp_path, group):
    admin, _, sent, files = prepare(app, tmp_path, group)
    first, first_button = complete_and_offer(app, admin, group, "first", sent)
    assert files == []
    click(app, first_button, 9101)
    drain(app)
    assert len(files) == 1
    first = admin.get("/api/funnel-runs/" + first["id"]).json
    assert first["data"]["order_status"] == "paid"
    assert first["data"]["guide_delivered_at"]
    first_order = first["data"]["order_id"]
    assert (
        admin.post("/api/funnel-runs/" + first["id"] + "/purchase", json={}).json["id"]
        == first_order
    )
    drain(app)
    assert len(files) == 1

    second, second_button = complete_and_offer(app, admin, group, "second", sent)
    assert len(files) == 1
    current = admin.get("/api/funnel-runs/" + second["id"]).json
    assert not current["data"].get("guide_right_id")
    assert not current["data"].get("guide_delivered_at")
    click(app, second_button, 9102)
    click(app, second_button, 9103)
    drain(app)
    current = admin.get("/api/funnel-runs/" + second["id"]).json
    assert current["data"]["order_status"] == "paid"
    assert current["data"]["guide_delivered_at"]
    assert current["data"]["order_id"] != first_order
    assert len(files) == 2
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='payment_attempt'").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM fulfillments").fetchone()[0] == 2
        assert (
            db.execute("SELECT count(*) FROM audit WHERE action='order.admin_access'").fetchone()[0]
            == 2
        )
        rows = db.execute(
            "SELECT data FROM objects WHERE kind='order' AND status='paid'"
        ).fetchall()
        import json

        assert all(json.loads(row["data"])["total"] == 123 for row in rows)
    assert not any(method in ("sendInvoice", "createInvoiceLink") for method, _ in sent)
    assert not any("email" in str(data).lower() for _, data in sent if data.get("chat_id") == 1)


def test_admin_resume_legacy_owned_shortcut_restores_offer_before_purchase(app, tmp_path):
    admin, _, sent, files = prepare(app, tmp_path)
    first, button = complete_and_offer(app, admin, "3-4", "first", sent)
    click(app, button, 9201)
    drain(app)
    first = admin.get("/api/funnel-runs/" + first["id"]).json
    second = finish(admin, start(admin, "3-4", key="legacy"), ["b"] * 6)
    with transaction(app.config["DATABASE"]) as db:
        old = unpack(db.execute("SELECT * FROM objects WHERE id=?", (second["id"],)).fetchone())
        update(
            db,
            old,
            app.clock[0],
            {**old["data"], "owned_guide_id": first["data"]["guide_right_id"]},
        )
    marker = len(sent)
    resumed = admin.post("/api/funnel-runs/" + second["id"] + "/resume", json={})
    assert resumed.status_code == 200
    assert "owned_guide_id" not in resumed.json["data"]
    drain(app)
    assert any("Забрать руководство за" in str(data) for _, data in sent[marker:])
    assert len(files) == 1
