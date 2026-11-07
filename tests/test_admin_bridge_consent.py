"""Operational admin previews do not depend on a customer's marketing choice."""

import json

import pytest

from marina.db import connect, transaction
from marina.funnel import notify_funnel
from tests.test_identity import app
from tests.test_admin_tripwire_flow import prepare, complete_and_offer
from tests.test_funnel import drain
from tests.test_privacy_documents import privacy


def delivery(app, tmp_path, group, choice):
    admin, _, sent, files = prepare(app, tmp_path, group)
    p = privacy(admin)
    response = admin.post(
        "/api/consents",
        json={"policy_id": p["id"], "accepted": True, "representative_confirmed": True},
    )
    assert response.status_code == 200, response.json
    if choice is not None:
        assert (
            admin.post(
                "/api/marketing-consents", json={"policy_id": p["id"], "accepted": choice}
            ).status_code
            == 200
        )
    run, _ = complete_and_offer(app, admin, group, "bridge-preview", sent)
    response = admin.post("/api/funnel-runs/" + run["id"] + "/purchase", json={})
    assert response.status_code == 200, response.json
    drain(app)
    assert len(files) == 1
    data = admin.get("/api/funnel-runs/" + run["id"]).json["data"]
    return admin, run, data, sent


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
@pytest.mark.parametrize("choice", [None, False])
def test_admin_receives_all_preview_bridges_without_marketing_opt_in(app, tmp_path, group, choice):
    _, run, data, sent = delivery(app, tmp_path, group, choice)
    anchor = data["guide_delivered_at"]
    marker = len(sent)
    for index, hours in enumerate((24, 48, 72), 1):
        app.clock[0] = anchor + index * 900 - 1
        drain(app)
        assert not any(f"Тестовое касание {hours}" in d.get("text", "") for _, d in sent[marker:])
        app.clock[0] += 1
        drain(app)
        assert sum(f"Тестовое касание {hours}" in d.get("text", "") for _, d in sent[marker:]) == 1
        with connect(app.config["DATABASE"]) as db:
            saved = json.loads(
                db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()["data"]
            )
        assert saved["followup_sent_at"][str(hours)] == app.clock[0]
        count = len(sent)
        notify_funnel(app, run["owner"], run["id"], f"funnel.followup.{hours}")
        assert len(sent) == count
    with connect(app.config["DATABASE"]) as db:
        choices = db.execute(
            "SELECT data FROM objects WHERE kind='marketing_consent' AND owner=?", (run["owner"],)
        ).fetchall()
    assert len(choices) == (0 if choice is None else 1)
    if choices:
        assert json.loads(choices[0]["data"])["accepted"] is False


@pytest.mark.parametrize("guard", ["removed_admin", "blocked", "stopped", "booked"])
def test_preview_opt_in_exception_preserves_delivery_guards(app, tmp_path, guard):
    admin, run, data, sent = delivery(app, tmp_path, "3-4", None)
    if guard == "removed_admin":
        app.config["ADMIN_TELEGRAM_IDS"] = set()
    elif guard == "blocked":
        with transaction(app.config["DATABASE"]) as db:
            db.execute("UPDATE accounts SET blocked=1 WHERE id=?", (run["owner"],))
    elif guard == "stopped":
        assert admin.post("/api/funnel/stop", json={}).status_code == 200
    else:
        assert (
            admin.post(
                "/api/funnel-runs/" + run["id"] + "/book", json={"preferences": "Synthetic slot"}
            ).status_code
            == 200
        )
    marker = len(sent)
    app.clock[0] = data["guide_delivered_at"] + 900
    drain(app)
    assert not any("Тестовое касание" in d.get("text", "") for _, d in sent[marker:])


def test_recover_previously_skipped_jobs_through_admin_retry_and_retiming(app, tmp_path):
    from marina.admin_followup_migration import migrate

    admin, run, data, sent = delivery(app, tmp_path, "5-6", None)
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "UPDATE outbox SET status='sent' WHERE object_id=? AND event LIKE 'funnel.followup.%'",
            (run["id"],),
        )
    app.clock[0] += 3 * 3600
    with connect(app.config["DATABASE"]) as db:
        jobs = db.execute(
            "SELECT id FROM outbox WHERE object_id=? AND event LIKE 'funnel.followup.%' ORDER BY event",
            (run["id"],),
        ).fetchall()
    for job in jobs:
        response = admin.post(
            "/api/admin/outbox/" + job["id"] + "/retry",
            json={"reason": "Owner requested restoration of skipped admin preview"},
        )
        assert response.status_code == 200, response.json
    assert migrate(app, "Owner requested restored admin previews at 15 minute steps") == 1
    anchor = app.clock[0]
    marker = len(sent)
    for index, hours in enumerate((24, 48, 72), 1):
        app.clock[0] = anchor + index * 900
        drain(app)
        assert sum(f"Тестовое касание {hours}" in d.get("text", "") for _, d in sent[marker:]) == 1
    assert migrate(app, "Repeated recovery must leave the existing schedule") == 0
