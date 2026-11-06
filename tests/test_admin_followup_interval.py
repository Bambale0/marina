"""Admin preview timing through the purchase API and real delivery outbox."""

import pytest

from marina import create_app
from tests.test_identity import app
from tests.test_admin_tripwire_flow import prepare, complete_and_offer
from tests.test_funnel import drain
from tests.test_funnel_followup_timing import followup_schedule


def deliver(app, tmp_path):
    admin, _, sent, files = prepare(app, tmp_path)
    run, _ = complete_and_offer(app, admin, "3-4", "timing", sent)
    response = admin.post("/api/funnel-runs/" + run["id"] + "/purchase", json={})
    assert response.status_code == 200, response.json
    drain(app)
    assert len(files) == 1
    data = admin.get("/api/funnel-runs/" + run["id"]).json["data"]
    return admin, run, data, sent


def test_admin_followups_use_fifteen_minute_intervals_and_survive_restart(app, tmp_path):
    admin, run, data, sent = deliver(app, tmp_path)
    anchor = data["guide_delivered_at"]
    assert followup_schedule(app, run["id"]) == [
        (f"funnel.followup.{hours}", anchor + index * 900)
        for index, hours in enumerate((24, 48, 72), 1)
    ]
    assert data["consultation_expires"] == anchor + 7 * 86400
    restarted = create_app(dict(app.config))
    restarted.clock = app.clock
    for index, hours in enumerate((24, 48, 72), 1):
        app.clock[0] = anchor + index * 900 - 1
        marker = len(sent)
        drain(restarted)
        assert not any("Тестовое касание" in d.get("text", "") for _, d in sent[marker:])
        app.clock[0] += 1
        drain(restarted)
        assert sum(f"Тестовое касание {hours}" in d.get("text", "") for _, d in sent[marker:]) == 1


def test_admin_downtime_skips_obsolete_preview_followup(app, tmp_path):
    _, _, data, sent = deliver(app, tmp_path)
    app.clock[0] = data["guide_delivered_at"] + 1800
    marker = len(sent)
    drain(app)
    messages = [d.get("text", "") for _, d in sent[marker:]]
    assert not any("Тестовое касание 24" in text for text in messages)
    assert sum("Тестовое касание 48" in text for text in messages) == 1


@pytest.mark.parametrize("stop", ["stop", "book"])
def test_admin_preview_cancellation_still_wins(app, tmp_path, stop):
    admin, run, data, sent = deliver(app, tmp_path)
    path = "/api/funnel/stop" if stop == "stop" else f"/api/funnel-runs/{run['id']}/book"
    response = admin.post(path, json={} if stop == "stop" else {"preferences": "Завтра вечером"})
    assert response.status_code == 200, response.json
    marker = len(sent)
    app.clock[0] = data["guide_delivered_at"] + 2700
    drain(app)
    assert not any("Тестовое касание" in d.get("text", "") for _, d in sent[marker:])
