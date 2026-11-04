import pytest

from tests.test_identity import app, login
from tests.test_screening import subject
from tests.test_training import exercises


@pytest.mark.parametrize("repeat_key", ["last-day", "new-request"])
def test_start_lesson_rechecks_expired_access_for_existing_lesson(app, repeat_key):
    admin, client = login(app), login(app, 2)
    child = subject(client, "child")
    exercises(app, admin)
    payload = {"subject_id": child["id"], "equipment": [], "constraints": []}
    first = client.post("/api/lessons/start", json={**payload, "key": "first-day"})
    assert first.status_code == 200, first.json
    trial = client.get("/api/me").json["trial"]
    app.clock[0] = trial["ends"] - 1
    last = client.post("/api/lessons/start", json={**payload, "key": "last-day"})
    assert last.status_code == 200, last.json
    app.clock[0] = trial["ends"]
    assert client.get("/api/lessons/" + last.json["id"]).status_code == 403
    repeated = client.post("/api/lessons/start", json={**payload, "key": repeat_key})
    assert repeated.status_code == 403, repeated.json
    assert "exercises" not in repeated.json
    assert client.get("/api/me").json["trial"] == trial
    right = admin.post(
        "/api/admin/rights/grant",
        json={
            "subject_id": child["id"],
            "service": "subscription",
            "starts": app.clock[0],
            "ends": app.clock[0] + 3600,
            "reason": "Restore access after trial",
            "key": "restore-subscription",
        },
    )
    assert right.status_code == 200, right.json
    restored = client.post("/api/lessons/start", json={**payload, "key": repeat_key})
    assert restored.status_code == 200, restored.json
    assert restored.json["id"] == last.json["id"]
    revoked = admin.post(
        "/api/admin/rights/" + right.json["id"] + "/revoke",
        json={"revision": right.json["revision"], "reason": "Verify revoked access"},
    )
    assert revoked.status_code == 200, revoked.json
    assert client.post("/api/lessons/start", json={**payload, "key": repeat_key}).status_code == 403
