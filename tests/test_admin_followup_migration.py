"""One-time retiming of existing administrative preview followups."""

import pytest

from marina import object_get
from marina.db import connect, emit, insert, transaction
from tests.test_identity import app, login


def seed(
    app, *, uid=1, run_status="completed", flags=None, statuses=("sent", "pending", "pending")
):
    client = login(app, uid)
    owner = client.get("/api/me").json["account"]["id"]
    now = app.clock[0]
    data = {
        "guide_delivered_at": now - 3600,
        "consultation_expires": now + 6 * 86400,
        "snapshot": {
            "followups": [{"hours": h, "text": "Synthetic"} for h in (24, 48, 72)],
            "consultation_amount_rub": 1234,
        },
        "order_id": "preserve-financial-reference",
    }
    data.update(flags or {})
    with transaction(app.config["DATABASE"]) as db:
        run = insert(db, "funnel_run", data, now - 3600, owner, status=run_status)
        for hours, status in zip((24, 48, 72), statuses):
            event = f"funnel.followup.{hours}"
            emit(db, owner, run["id"], event, now - 3600 + hours * 3600)
            db.execute(
                "UPDATE outbox SET status=? WHERE object_id=? AND event=?",
                (status, run["id"], event),
            )
        # An unrelated event and order must remain unchanged.
        emit(db, owner, run["id"], "funnel.offer", now + 10000)
        order = insert(db, "order", {"total": 5678, "currency": "RUB"}, now, owner, status="paid")
    return run, order


def state(app, run_id):
    with connect(app.config["DATABASE"]) as db:
        run = object_get(db, run_id)
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM outbox WHERE object_id=? ORDER BY event", (run_id,)
            )
        ]
        audit_rows = [
            dict(row) for row in db.execute("SELECT * FROM audit WHERE object_id=?", (run_id,))
        ]
        return run, jobs, audit_rows


def test_only_remaining_admin_jobs_retime_once_with_audit_and_preserved_terms(app):
    from marina.admin_followup_migration import migrate

    run, order = seed(app)
    before, old_jobs, _ = state(app, run["id"])
    now = app.clock[0]
    assert migrate(app, "Owner requested 15-minute administrative previews") == 1
    after, jobs, history = state(app, run["id"])
    old_by_event = {job["event"]: job for job in old_jobs}
    by_event = {job["event"]: job for job in jobs}
    assert by_event["funnel.followup.48"]["available"] == now + 900
    assert by_event["funnel.followup.72"]["available"] == now + 1800
    for event in ("funnel.followup.24", "funnel.offer"):
        assert by_event[event] == old_by_event[event]
    assert after["data"]["followup_due_at"] == {
        "24": before["data"]["guide_delivered_at"] + 24 * 3600,
        "48": now + 900,
        "72": now + 1800,
    }
    assert after["data"]["guide_delivered_at"] == before["data"]["guide_delivered_at"]
    assert after["data"]["consultation_expires"] == before["data"]["consultation_expires"]
    assert after["data"]["snapshot"] == before["data"]["snapshot"]
    assert after["data"]["order_id"] == before["data"]["order_id"]
    assert after["revision"] == before["revision"] + 1
    assert len(history) == 1
    assert history[0]["reason"] == "Owner requested 15-minute administrative previews"
    with connect(app.config["DATABASE"]) as db:
        assert object_get(db, order["id"]) == order
    app.clock[0] += 600
    assert migrate(app, "Repeated deployment") == 0
    assert state(app, run["id"]) == (after, jobs, history)


@pytest.mark.parametrize(
    "case",
    [
        "client",
        "removed_admin",
        "blocked",
        "stopped",
        "booked",
        "expired",
        "undelivered",
        "incomplete",
        "cancelled",
        "processing",
    ],
)
def test_migration_does_not_resurrect_or_accelerate_ineligible_runs(app, case):
    from marina.admin_followup_migration import migrate

    flags = {
        "stopped": {"notifications_stopped": True},
        "booked": {"booking_id": "fixture-booking"},
        "expired": {"consultation_expires": app.clock[0]},
        "undelivered": {"guide_delivered_at": None},
    }.get(case, {})
    statuses = {
        "cancelled": ("cancelled", "cancelled", "cancelled"),
        "processing": ("processing", "sent", "cancelled"),
    }.get(case, ("pending", "pending", "pending"))
    run, _ = seed(
        app,
        uid=2 if case == "client" else 1,
        flags=flags,
        run_status="active" if case == "incomplete" else "completed",
        statuses=statuses,
    )
    if case == "removed_admin":
        app.config["ADMIN_TELEGRAM_IDS"] = set()
    if case == "blocked":
        with transaction(app.config["DATABASE"]) as db:
            db.execute("UPDATE accounts SET blocked=1 WHERE id=?", (run["owner"],))
    before = state(app, run["id"])
    assert migrate(app, "Owner requested admin preview retiming") == 0
    assert state(app, run["id"]) == before


def test_three_pending_followups_use_fifteen_thirty_fortyfive_minutes(app):
    from marina.admin_followup_migration import migrate

    run, _ = seed(app, statuses=("pending", "pending", "pending"))
    now = app.clock[0]
    assert migrate(app, "Owner requested admin preview retiming") == 1
    _, jobs, _ = state(app, run["id"])
    assert [job["available"] for job in jobs if job["event"].startswith("funnel.followup.")] == [
        now + 900,
        now + 1800,
        now + 2700,
    ]


def test_invalid_reason_does_not_modify_jobs(app):
    from marina.admin_followup_migration import migrate
    from marina import Problem

    run, _ = seed(app)
    before = state(app, run["id"])
    with pytest.raises(Problem):
        migrate(app, "")
    assert state(app, run["id"]) == before
