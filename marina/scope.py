"""Published operations policy controls new services without erasing history."""

from . import require
from .commerce import policies

SERVICES = ("pdf", "case", "subscription", "course", "booking", "guide", "video_task")


def enabled_services(db):
    policy = policies(db, "operations")
    configured = policy["data"].get("enabled_services") if policy else None
    return list(SERVICES if configured is None else configured)


def service_enabled(db, service):
    return service in enabled_services(db)


def require_service(db, service):
    require(service_enabled(db, service), "Эта услуга больше не предлагается", 403)


def action_service(action, data):
    if action == "training":
        return "subscription"
    if action in ("case_prepare", "case_submit"):
        return "case"
    if action in ("products", "quote"):
        return data.get("service")
    return None
