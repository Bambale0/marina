"""Explicit client projections: drafts and specialist notes never leave admin APIs."""

import copy


def client_view(obj):
    obj = copy.deepcopy(obj)
    d, kind = obj["data"], obj["kind"]
    if kind == "funnel_run":
        from .funnel import public_run

        return public_run(obj)
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
