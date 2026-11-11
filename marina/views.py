"""Explicit client projections: drafts and specialist notes never leave admin APIs."""

import copy


def client_view(obj):
    obj = copy.deepcopy(obj)
    d, kind = obj["data"], obj["kind"]
    if kind == "funnel_run":
        from .funnel import public_run

        return public_run(obj)
    if kind == "consultation_result":
        obj["data"] = {
            key: value
            for key, value in d.items()
            if key
            in (
                "booking_id",
                "title",
                "filename",
                "result_delivery",
                "completed_at",
                "due_at",
                "answer_attachments",
                "published_at",
                "delivered_at",
                "delivery_channel",
            )
        }
        return obj
    if kind == "video_task":
        obj["data"] = {
            k: v
            for k, v in d.items()
            if k
            in (
                "lead_id",
                "amount_rub",
                "rules",
                "assignment",
                "videos",
                "recommendations",
                "due_at",
                "assigned_at",
                "submitted_at",
                "answered_at",
                "answer_attachments",
            )
        }
        obj["data"]["videos"] = [{"slot": v["slot"]} for v in d.get("videos", [])]
        payment = d.get("manual_payment") or {}
        confirmed = payment.get("status") == "confirmed"
        obj["data"]["manual_payment"] = {
            "status": "confirmed" if confirmed else "unknown",
            "amount_rub": d.get("amount_rub"),
            "confirmed_at": payment.get("confirmed_at") if confirmed else None,
        }
        obj["data"]["payment_status"] = (
            "confirmed"
            if confirmed or (type(d.get("paid_at")) is int and d["paid_at"] > 0)
            else "pending"
        )
        return obj
    if kind == "video_review":
        from .crm import client_review

        return client_review(obj)
    if kind == "crm_message":
        obj["data"] = {}
        return obj
    if kind == "report":
        for key in ("revisions", "approved_revision", "approved_by", "approved_at"):
            d.pop(key, None)
    if kind == "lesson":
        d.pop("exclusions", None)
    if kind == "case":
        d.pop("notes", None)
    for key in ("product_snapshot", "method_snapshot"):
        if isinstance(d.get(key), dict):
            d[key].pop("approval", None)
    if isinstance(d.get("offer_snapshot"), dict):
        d["offer_snapshot"] = {
            key: d["offer_snapshot"][key] for key in ("title", "text") if key in d["offer_snapshot"]
        }
    return obj


def catalog_view(obj):
    obj = copy.deepcopy(obj)
    d = obj["data"]
    d.pop("approval", None)
    if obj["kind"] == "course":
        d["lessons"] = [
            {"id": lesson["id"], "title": lesson["title"]} for lesson in d.get("lessons", [])
        ]
    if obj["kind"] == "exercise":
        d.pop("video_id", None)
        d.pop("instructions", None)
    if obj["kind"] == "method":
        d = {
            k: v
            for k, v in d.items()
            if k
            in (
                "title",
                "audience",
                "min_months",
                "max_months",
                "source",
                "rights",
                "limitations",
            )
        }
        obj["data"] = d
    return obj


def history_view(obj):
    obj = client_view(obj)
    if obj["kind"] == "lesson":
        obj["data"]["exercises"] = [
            {"id": e["id"], "title": e["title"]} for e in obj["data"].get("exercises", [])
        ]
    return obj
