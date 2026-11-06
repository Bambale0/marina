"""Idempotent maintenance of already queued administrative preview followups."""

from . import require
from .db import audit, transaction, unpack, update


def migrate(app, reason):
    """Retime remaining pending admin jobs once; return the number of changed runs.

    Run with the worker stopped so it cannot deliver a job while its preview
    schedule is being replaced. Completed/cancelled jobs and commercial deadlines
    remain unchanged. Account roles are not trusted: the current Telegram
    administrator allowlist determines eligibility.
    """
    require(
        isinstance(reason, str) and 3 <= len(reason.strip()) <= 1000,
        "Укажите причину изменения графика (3–1000 символов)",
    )
    now = int(app.config["CLOCK"]())
    changed = 0
    with transaction(app.config["DATABASE"]) as db:
        rows = db.execute(
            "SELECT o.*,a.telegram_id FROM objects o JOIN accounts a ON a.id=o.owner "
            "WHERE o.kind='funnel_run' AND o.status='completed' AND a.blocked=0"
        ).fetchall()
        for row in rows:
            if row["telegram_id"] not in app.config["ADMIN_TELEGRAM_IDS"]:
                continue
            obj = unpack(row)
            d = obj["data"]
            if (
                d.get("admin_followup_retimed_at") is not None
                or not d.get("guide_delivered_at")
                or d.get("notifications_stopped")
                or d.get("booking_id")
                or d.get("consultation_expires", 0) <= now
            ):
                continue
            hours = [f["hours"] for f in d.get("snapshot", {}).get("followups", [])]
            if not hours:
                continue
            order = {f"funnel.followup.{h}": index for index, h in enumerate(hours)}
            jobs = [
                job
                for job in db.execute(
                    "SELECT id,event FROM outbox WHERE owner=? AND object_id=? "
                    "AND status='pending' AND event LIKE 'funnel.followup.%'",
                    (obj["owner"], obj["id"]),
                )
                if job["event"] in order
            ]
            if not jobs:
                continue
            jobs.sort(key=lambda job: order[job["event"]])
            previous = d.get("followup_due_at") or {}
            due_times = {
                str(h): previous.get(str(h), d["guide_delivered_at"] + h * 3600) for h in hours
            }
            for index, job in enumerate(jobs, 1):
                at = now + index * 900
                db.execute(
                    "UPDATE outbox SET available=? WHERE id=? AND status='pending'",
                    (at, job["id"]),
                )
                due_times[job["event"].rsplit(".", 1)[-1]] = at
            result = update(
                db,
                obj,
                now,
                {**d, "followup_due_at": due_times, "admin_followup_retimed_at": now},
            )
            audit(
                db,
                "system:admin-followup-migration",
                "funnel.admin_followups.retimed",
                result,
                now,
                reason.strip(),
                obj["revision"],
            )
            changed += 1
    return changed
