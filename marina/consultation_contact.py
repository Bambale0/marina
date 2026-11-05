"""A paid contact deadline is separate from a specialist's manual acknowledgement."""

import copy
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g

from . import body, object_get, protected, reason, require
from .db import audit, connect, emit, unpack, update


PENDING_CONTACT_SQL = (
    "kind='booking' AND status!='cancelled' "
    "AND json_extract(data,'$.paid_at') IS NOT NULL "
    "AND json_extract(data,'$.contact_due_at') IS NOT NULL "
    "AND json_extract(data,'$.contacted_at') IS NULL"
)


def validate_contact_service(spec):
    require(isinstance(spec, dict), "Настройте срок личного контакта после оплаты")
    require(
        isinstance(spec.get("version"), str) and 0 < len(spec["version"].strip()) <= 200,
        "Укажите версию правил личного контакта",
    )
    try:
        ZoneInfo(spec.get("timezone", ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        require(False, "Укажите часовой пояс IANA для личного контакта")
    require(
        type(spec.get("start_hour")) is int
        and type(spec.get("end_hour")) is int
        and 0 <= spec["start_hour"] < spec["end_hour"] <= 24,
        "Укажите рабочее окно личного контакта в пределах суток",
    )
    require(
        type(spec.get("due_minutes")) is int and 0 < spec["due_minutes"] <= 7 * 24 * 60,
        "Укажите срок личного контакта в рабочих минутах",
    )
    require(
        type(spec.get("source_page")) is int and spec["source_page"] > 0,
        "Укажите страницу основания личного контакта",
    )


def contact_due_at(paid_at, spec):
    """Count configured working minutes each day; persist the resulting UTC timestamp."""
    validate_contact_service(spec)
    zone = ZoneInfo(spec["timezone"])
    current = datetime.fromtimestamp(paid_at, zone)
    remaining = spec["due_minutes"] * 60
    while remaining:
        day = current.date()
        opens = datetime.combine(day, time(spec["start_hour"]), zone)
        closes = (
            datetime.combine(day + timedelta(days=1), time.min, zone)
            if spec["end_hour"] == 24
            else datetime.combine(day, time(spec["end_hour"]), zone)
        )
        if current < opens:
            current = opens
        if current >= closes:
            current = datetime.combine(day + timedelta(days=1), time(spec["start_hour"]), zone)
            continue
        available = int(closes.timestamp() - current.timestamp())
        if remaining <= available:
            return int(current.timestamp()) + remaining
        remaining -= available
        current = datetime.combine(day + timedelta(days=1), time(spec["start_hour"]), zone)


def paid_contact_fields(booking, order, paid_at):
    """Only the frozen paid order can start the obligation; legacy grants stay untouched."""
    data = order["data"]
    if (
        data.get("access_source") == "admin"
        or not data.get("consultation_run_id")
        or booking["data"].get("contact_due_at") is not None
    ):
        return {}
    journey = data.get("booking_snapshot", {}).get("data", {}).get("journey_snapshot", {})
    spec = journey.get("contact_service")
    if not spec:
        return {}
    return {
        "contact_service": copy.deepcopy(spec),
        "contact_due_at": contact_due_at(paid_at, spec),
        "contacted_at": None,
        "contacted_by": None,
    }


def contact_pending(booking):
    return (
        booking["kind"] == "booking"
        and booking["status"] != "cancelled"
        and booking["data"].get("paid_at") is not None
        and booking["data"].get("contact_due_at") is not None
        and booking["data"].get("contacted_at") is None
    )


def tick_contacts(db, now):
    for row in db.execute(
        "SELECT * FROM objects WHERE ("
        + PENDING_CONTACT_SQL
        + ") AND json_extract(data,'$.contact_due_at')<=?",
        (now,),
    ).fetchall():
        booking = unpack(row)
        marker = (
            "consultation-contact-due:"
            + booking["id"]
            + ":"
            + str(booking["data"]["contact_due_at"])
        )
        if not db.execute("SELECT 1 FROM runtime WHERE key=?", (marker,)).fetchone():
            emit(db, booking["owner"], booking["id"], "consultation.contact_due", now)
            db.execute("INSERT INTO runtime VALUES(?,?)", (marker, str(now)))


def notify_contact_due(app, owner, id):
    from .consultation_delivery import send_once
    from .providers import telegram

    with connect(app.config["DATABASE"]) as db:
        booking = object_get(db, id, "booking", owner)
    if not contact_pending(booking) or booking["data"]["contact_due_at"] > int(
        app.config["CLOCK"]()
    ):
        return
    require(app.config["ADMIN_TELEGRAM_IDS"], "Укажите специалиста для уведомлений")
    for chat in sorted(app.config["ADMIN_TELEGRAM_IDS"]):
        marker = (
            "consultation-contact:"
            + id
            + ":"
            + str(booking["data"]["contact_due_at"])
            + ":admin:"
            + str(chat)
        )
        send_once(
            app,
            marker,
            lambda chat=chat: telegram(
                app,
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": "Личный контакт после оплаты ещё не отмечен. Свяжитесь с родителем и отметьте состоявшийся контакт в заявке. Откройте /queue.",
                },
            ),
        )


def register_consultation_contact(app):
    @app.post("/api/admin/bookings/<id>/contacted")
    @protected(admin=True)
    def contacted(id):
        booking = object_get(g.db, id, "booking")
        data = body()
        why = reason(data)
        require(booking["revision"] == data.get("revision"), "Заявка изменена, обновите её", 409)
        require(booking["status"] != "cancelled", "Запись отменена", 409)
        require(
            booking["data"].get("paid_at") is not None
            and booking["data"].get("contact_due_at") is not None,
            "Для заявки нет обязательства личного контакта после оплаты",
        )
        order = object_get(g.db, booking["data"].get("order_id"), "order", booking["owner"])
        require(
            order["status"] == "paid" and order["data"].get("access_source") != "admin",
            "Нет подтверждённой оплаты консультации",
        )
        if booking["data"].get("contacted_at") is not None:
            return booking
        result = update(
            g.db,
            booking,
            g.now,
            {**booking["data"], "contacted_at": g.now, "contacted_by": g.actor["id"]},
        )
        audit(g.db, g.actor["id"], "booking.contacted", result, g.now, why, booking["revision"])
        return result
