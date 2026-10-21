"""Operational reports from immutable business facts, without client answers."""

from flask import g, request
from . import Problem, protected
from .db import unpack


def register_analytics(app):
    @app.get("/api/admin/analytics")
    @protected(admin=True)
    def analytics():
        try:
            start = int(request.args.get("from", g.now - 30 * 86400))
            end = int(request.args.get("to", g.now + 1))
        except ValueError:
            raise Problem("Неверный период")
        if start >= end:
            raise Problem("Начало периода должно предшествовать концу")
        funnel = {
            row["action"]: row["count"]
            for row in g.db.execute(
                "SELECT action,count(DISTINCT object_id) AS count FROM audit WHERE at>=? AND at<? GROUP BY action",
                (start, end),
            )
        }
        receipts = [
            dict(row)
            for row in g.db.execute(
                "SELECT currency,status,sum(amount) AS amount,count(*) AS count FROM receipts WHERE received_at>=? AND received_at<? GROUP BY currency,status",
                (start, end),
            )
        ]
        sla = {}
        for kind, action in [
            ("report", "report.publish"),
            ("case", "case.answer"),
            ("case", "case.followup-answer"),
        ]:
            durations = []
            overdue = 0
            for row in g.db.execute(
                "SELECT o.*,a.finished FROM objects o JOIN (SELECT object_id,min(at) AS finished FROM audit WHERE action=? GROUP BY object_id) a ON a.object_id=o.id WHERE o.kind=? AND a.finished>=? AND a.finished<?",
                (action, kind, start, end),
            ):
                obj = unpack(row)
                due = (
                    obj["data"].get("followup", {}).get("due_at")
                    if action.endswith("followup-answer")
                    else obj["data"].get("due_at")
                )
                if due:
                    durations.append(max(0, obj["finished"] - (due - 48 * 3600)))
                    overdue += int(obj["finished"] > due)
            sla[action] = {
                "completed": len(durations),
                "overdue": overdue,
                "mean_hours": round(sum(durations) / len(durations) / 3600, 2)
                if durations
                else None,
            }
        return {
            "from": start,
            "to": end,
            "funnel": funnel,
            "receipts": receipts,
            "receipt_period_basis": "received_at",
            "sla": sla,
            "new_accounts": g.db.execute(
                "SELECT count(*) FROM accounts WHERE created>=? AND created<?", (start, end)
            ).fetchone()[0],
        }
