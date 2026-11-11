import pytest

from marina import Problem
from marina.bot import LEGACY_MENU, MENU, Bot
from tests.test_identity import app, login
from tests.test_screening import publish, subject
from tests.test_commerce import product, policy, quote


def restrict(admin):
    return publish(
        admin,
        "policy",
        {
            "type": "operations",
            "terms": "Owner removed training, subscription and asynchronous cases",
            "pause_sales": False,
            "max_open_orders": 0,
            "enabled_services": ["pdf", "course", "guide", "booking"],
        },
    )


def test_scope_filters_catalog_and_new_quote_and_saved_order(app):
    admin, owner = login(app), login(app, 2)
    child = subject(owner, "child")
    policy(admin)
    subscription = product(admin)
    calculation = quote(owner, subscription, [child])
    assert calculation.status_code == 201, calculation.json
    restrict(admin)
    assert "subscription" not in owner.get("/api/me").json["enabled_services"]
    assert all(
        p["data"]["service"] != "subscription"
        for p in owner.get("/api/catalog/product").json["items"]
    )
    assert quote(owner, subscription, [child]).status_code == 403
    assert (
        owner.post(
            "/api/orders", json={"quote_id": calculation.json["id"], "key": "stale-quote"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("path", ["/api/cases", "/api/plans", "/api/lessons/start"])
def test_disabled_service_rejects_direct_creation_before_collecting_data(app, path):
    admin, owner = login(app), login(app, 2)
    restrict(admin)
    assert owner.post(path, json={}).status_code == 403


def test_bot_menu_and_old_action_obey_scope(app):
    restrict(login(app))
    sent = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: (
        sent.append(data) or {"ok": True, "result": {}}
    )
    bot = Bot(app, {"id": 2, "first_name": "Fixture"}, 2, "scope-test")
    bot.send("Меню", MENU)
    labels = [b["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == [
        "👥 Вступить в группу",
        "💬 Связаться со специалистом",
        "📅 Записаться на онлайн-занятие",
        "💳 Мой кошелёк",
    ]
    # The existing service scope must still apply to the old sections.
    bot.send("Прежние разделы", LEGACY_MENU)
    legacy_labels = [b["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "Занятия" not in legacy_labels
    assert "Консультация" in legacy_labels
    with pytest.raises(Problem):
        bot.action("training", {})


def test_public_policy_has_no_authentication_or_private_metadata(app):
    admin = login(app)
    published = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Published policy <script>unsafe</script>",
            "consent_text": "Separate consent text",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    visitor = app.test_client()
    page = visitor.get("/privacy")
    assert page.status_code == 200
    assert b"<script>unsafe</script>" not in page.data
    assert b"&lt;script&gt;unsafe&lt;/script&gt;" in page.data
    data = visitor.get("/api/privacy/current").json
    assert data["id"] == published["id"]
    assert data["consent_text"] == "Separate consent text"
    assert "approval" not in str(data)
