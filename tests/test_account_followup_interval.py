"""Scoped follow-up timing configured through the existing audited admin policy API."""

import pytest

from marina import create_app
from marina.db import connect
from marina.funnel import followup_times
from tests.test_identity import app, login
from tests.test_screening import publish
from tests.test_commerce import pay
from tests.test_funnel import drain, private_source, setup_sale
from tests.test_funnel_followup_timing import followup_schedule


def configure(admin, account_id, seconds=300):
    return publish(
        admin,
        "policy",
        {
            "type": "operations",
            "terms": "Synthetic owner-approved account timing override",
            "followup_interval_overrides": {account_id: seconds},
        },
    )


def delivery(app):
    admin, owner, run, _, _, sent, files = setup_sale(app)
    account_id = owner.get("/api/me").json["account"]["id"]
    configure(admin, account_id)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    data = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    return admin, owner, run, data, sent, files


def test_client_gets_five_minute_steps_without_admin_access_and_survives_restart(app):
    _, owner, run, data, sent, files = delivery(app)
    anchor = data["guide_delivered_at"]
    assert followup_schedule(app, run["id"]) == [
        (f"funnel.followup.{h}", anchor + i * 300) for i, h in enumerate((24, 48, 72), 1)
    ]
    assert data["consultation_expires"] == anchor + 7 * 86400
    assert owner.get("/api/me").json["account"]["role"] == "client"
    assert owner.get("/api/admin/accounts").status_code == 403
    assert len(files) == 1
    restarted = create_app(dict(app.config))
    for index, hours in enumerate((24, 48, 72), 1):
        marker = len(sent)
        app.clock[0] = anchor + index * 300 - 1
        drain(restarted)
        assert not any("Тестовое касание" in d.get("text", "") for _, d in sent[marker:])
        app.clock[0] += 1
        drain(restarted)
        drain(restarted)
        assert sum(f"Тестовое касание {hours}" in d.get("text", "") for _, d in sent[marker:]) == 1


def test_override_does_not_change_other_accounts_or_existing_schedules(app):
    admin, owner, run, _, _, _, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    previous = followup_schedule(app, run["id"])
    account_id = owner.get("/api/me").json["account"]["id"]
    stranger_id = login(app, 3).get("/api/me").json["account"]["id"]
    configure(admin, account_id)
    snapshot = {"followups": [{"hours": h} for h in (24, 48, 72)]}
    with connect(app.config["DATABASE"]) as db:
        target = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        stranger = db.execute("SELECT * FROM accounts WHERE id=?", (stranger_id,)).fetchone()
        administrator = db.execute("SELECT * FROM accounts WHERE telegram_id=1").fetchone()
    assert list(followup_times(app, target, snapshot, 1000).values()) == [1300, 1600, 1900]
    assert list(followup_times(app, stranger, snapshot, 1000).values()) == [
        1000 + h * 3600 for h in (24, 48, 72)
    ]
    assert list(followup_times(app, administrator, snapshot, 1000).values()) == [1900, 2800, 3700]
    assert followup_schedule(app, run["id"]) == previous


@pytest.mark.parametrize("seconds", [True, "300", 0, -1, 59, 86401])
def test_admin_policy_rejects_invalid_interval(app, seconds):
    admin, owner = login(app), login(app, 2)
    account_id = owner.get("/api/me").json["account"]["id"]
    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "policy",
            "title": "Synthetic timing",
            "data": {
                "type": "operations",
                "terms": "Synthetic timing validation",
                "followup_interval_overrides": {account_id: seconds},
            },
            "reason": "Synthetic validation test",
        },
    ).json
    response = admin.post(
        f"/api/admin/content/{draft['id']}/approve",
        json={"revision": draft["revision"], "reason": "Validate timing policy"},
    )
    assert response.status_code == 422


def test_admin_policy_rejects_unknown_account_and_client_cannot_edit(app):
    admin, owner = login(app), login(app, 2)
    payload = {
        "kind": "policy",
        "title": "Synthetic timing",
        "data": {
            "type": "operations",
            "terms": "Synthetic timing validation",
            "followup_interval_overrides": {"missing-account": 300},
        },
        "reason": "Synthetic validation test",
    }
    assert owner.post("/api/admin/content", json=payload).status_code == 403
    draft = admin.post("/api/admin/content", json=payload).json
    assert (
        admin.post(
            f"/api/admin/content/{draft['id']}/approve",
            json={"revision": draft["revision"], "reason": "Validate account reference"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("constraint", ["stop", "booking", "marketing"])
def test_timing_override_preserves_delivery_constraints(app, constraint):
    admin, owner, run, data, sent, _ = delivery(app)
    if constraint == "stop":
        assert owner.post("/api/funnel/stop", json={}).status_code == 200
    elif constraint == "booking":
        assert (
            owner.post(
                f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Synthetic slot"}
            ).status_code
            == 200
        )
    else:
        publish(
            admin,
            "policy",
            {
                "type": "privacy",
                "terms": "Synthetic marketing consent required",
                "media_max_bytes": 10000,
                "retention_days": 30,
                "require_marketing_consent": True,
            },
        )
    marker = len(sent)
    app.clock[0] = data["guide_delivered_at"] + 300
    drain(app)
    assert not any("Тестовое касание" in d.get("text", "") for _, d in sent[marker:])
