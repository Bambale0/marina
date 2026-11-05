"""Published consultation forms and qualified outside-age leads."""

import copy
from urllib.parse import urlencode, urlparse
from flask import g, Response
from . import body, object_get, protected, require, reason
from .commerce import policies
from .db import audit, emit, insert, update
from .screening import idempotent


BUTTON_KEYS = {
    "info",
    "book_promo",
    "book_regular",
    "book_target",
    "contact",
    "support",
    "contact_open",
    "pay",
    "back",
}


def display_price(amount):
    require(type(amount) is int and amount >= 0, "Проверьте сумму")
    rub, cents = divmod(amount, 100)
    return f"{rub:,}".replace(",", " ") + (f",{cents:02}" if cents else "") + " ₽"


def button_text(journey, key, price, fallback):
    return journey.get("buttons", {}).get(key, fallback).format(price=price)


def presentation_journey(db, journey, group):
    from .funnel import published

    result = copy.deepcopy(journey)
    if result and not result.get("buttons"):
        current = published(db, group)
        labels = current["data"].get("consultation_journey", {}).get("buttons") if current else None
        if labels:
            result["buttons"] = copy.deepcopy(labels)
    return result


def journey_for(db, run):
    from .funnel import published

    saved = run["data"]["snapshot"].get("consultation_journey")
    current = published(db, run["data"]["child_age"]) if not saved else None
    return presentation_journey(
        db,
        saved or (current["data"].get("consultation_journey") if current else None),
        run["data"]["child_age"],
    )


def order_payment_label(db, order):
    data = order["data"]
    run_id = data.get("consultation_run_id")
    if not run_id or data.get("currency") != "RUB":
        return "💳 Оплатить"
    run = object_get(db, run_id, "funnel_run", order["owner"])
    journey = data.get("booking_snapshot", {}).get("data", {}).get(
        "journey_snapshot"
    ) or journey_for(db, run)
    journey = presentation_journey(db, journey, run["data"]["child_age"])
    if not journey or not journey.get("buttons"):
        return "💳 Оплатить"
    return button_text(journey, "pay", display_price(data["total"]), "💳 Оплатить")


def material_ready(db, id):
    return bool(
        db.execute(
            "SELECT 1 FROM objects o JOIN blobs b ON b.id=json_extract(o.data,'$.asset_id') WHERE o.id=? AND o.kind='material' AND o.status='published' AND b.case_id=o.id AND b.mime='application/pdf' AND length(b.body)>0",
            (id,),
        ).fetchone()
    )


def validate_journey(db, d):
    require(isinstance(d, dict), "Проверьте сценарий записи")
    for key in (
        "version",
        "description",
        "checkout_body",
        "contact_text",
        "success_before_file",
        "success_after_file",
        "preparation_filename",
    ):
        require(
            isinstance(d.get(key), str) and 0 < len(d[key]) <= 16000,
            "Проверьте текст сценария: " + key,
        )
    require(
        d.get("payment_timing") in ("before_scheduling", "after_scheduling"),
        "Выберите порядок оплаты",
    )
    labels = d.get("buttons")
    if labels is not None:
        from string import Formatter

        require(
            isinstance(labels, dict) and set(labels) == BUTTON_KEYS, "Настройте тексты всех кнопок"
        )
        for label in labels.values():
            require(
                isinstance(label, str) and 0 < len(label.strip()) <= 120, "Проверьте текст кнопки"
            )
            try:
                parts = list(Formatter().parse(label))
                require(
                    all(
                        field is None or (field == "price" and not spec and conversion is None)
                        for _, field, spec, conversion in parts
                    ),
                    "В кнопке разрешена только переменная {price} без форматирования",
                )
                label.format(price="1 000 ₽")
            except (ValueError, KeyError, IndexError, AttributeError):
                require(False, "Проверьте переменные кнопки")
        require("{price}" in labels["pay"], "В кнопке оплаты нужна динамическая сумма {price}")
    qs = d.get("questions")
    require(
        isinstance(qs, list) and len(qs) == 3 and all(isinstance(q, dict) for q in qs),
        "В анкете нужны три вопроса",
    )
    require(
        [q.get("id") for q in qs] == ["profile", "problem", "slot"], "Проверьте порядок вопросов"
    )
    for i, q in enumerate(qs):
        require(isinstance(q.get("prompt"), str) and 0 < len(q["prompt"]) <= 3000, "Укажите вопрос")
        options = q.get("options")
        require(
            isinstance(options, list) and len(options) <= 10 and (i == 0 or options),
            "Проверьте варианты",
        )
        ids = []
        for o in options:
            require(
                isinstance(o, dict)
                and isinstance(o.get("id"), str)
                and 0 < len(o["id"]) <= 60
                and isinstance(o.get("label"), str)
                and 0 < len(o["label"]) <= 300,
                "Проверьте вариант ответа",
            )
            ids.append(o["id"])
        require(len(ids) == len(set(ids)), "Варианты должны иметь разные ID")
        require(type(q.get("allow_custom", False)) is bool, "Проверьте свободный ответ")
        require(
            q.get("allow_custom", False) == ("other" in ids),
            "Свободный ответ должен соответствовать варианту «Другое»",
        )
    if d["payment_timing"] == "before_scheduling":
        require(
            material_ready(db, d.get("preparation_material_id")),
            "Перед оплатой до согласования загрузите и опубликуйте авторскую PDF-памятку",
        )


def intake_payload(journey, answers):
    require(
        isinstance(answers, dict) and set(answers) == {"profile", "problem", "slot"},
        "Ответьте на три вопроса анкеты",
    )
    display = {}
    for q in journey["questions"]:
        value = answers[q["id"]]
        require(isinstance(value, str) and 0 < len(value.strip()) <= 2000, "Проверьте ответ анкеты")
        options = {o["id"]: o["label"] for o in q["options"] if o["id"] != "other"}
        if options and value not in options:
            require(
                q.get("allow_custom") is True
                and value.startswith("custom:")
                and bool(value[7:].strip()),
                "Выберите действующий вариант анкеты",
            )
        display[q["id"]] = options.get(
            value, value[7:] if value.startswith("custom:") else value
        ).strip()
    return copy.deepcopy(answers), display


def payable_booking(b):
    states = (
        ("requested", "proposed", "confirmed", "reschedule_requested")
        if b["data"].get("payment_timing") == "before_scheduling"
        else ("confirmed",)
    )
    return b["status"] in states and b["data"].get("paid_at") is None


def contact_url(url, text):
    require(
        urlparse(url).scheme == "https" and urlparse(url).hostname == "t.me",
        "Укажите контакт специалиста",
    )
    return url.rstrip("/") + "?" + urlencode({"text": text}) if text else url


def validate_routes(db, d):
    require(
        isinstance(d.get("menu_text"), str) and 0 < len(d["menu_text"]) <= 16000,
        "Добавьте меню другого возраста",
    )
    contact_url(d.get("contact_url", ""), "")
    require(
        isinstance(d.get("specialist_name"), str) and bool(d["specialist_name"]),
        "Укажите имя специалиста",
    )
    rs = d.get("routes")
    require(
        isinstance(rs, dict) and set(rs) == {"baby_0_2", "teen_senior", "adult"},
        "Настройте три категории",
    )
    for r in rs.values():
        require(
            isinstance(r, dict)
            and all(
                isinstance(r.get(k), str) and 0 < len(r[k]) <= 16000
                for k in ("label", "text", "booking_label")
            ),
            "Проверьте тексты категории",
        )
        require(type(r.get("amount_rub")) is int and r["amount_rub"] > 0, "Укажите цену категории")
        if "contact_label" in r:
            require(
                isinstance(r["contact_label"], str) and 0 < len(r["contact_label"].strip()) <= 120,
                "Проверьте кнопку связи",
            )
        if r.get("material_id"):
            require(
                all(
                    isinstance(r.get(k), str) and bool(r[k].strip())
                    for k in ("gift_text", "material_filename")
                ),
                "Добавьте текст выдачи и имя PDF категории",
            )
            require(material_ready(db, r["material_id"]), "Опубликуйте PDF категории")
    baby = rs["baby_0_2"]
    require(
        all(
            type(baby.get(k)) is int and baby[k] > 0
            for k in ("required_videos", "max_video_seconds", "response_hours")
        ),
        "Укажите количество видео, длительность и срок разбора",
    )
    require(
        baby["required_videos"] <= 6 and baby["max_video_seconds"] <= 600,
        "Проверьте ограничения задания",
    )


def register_consultation_journey(app):
    @app.get("/api/admin/consultation-route-defaults")
    @protected(admin=True)
    def route_defaults():
        import json
        from pathlib import Path

        p = policies(g.db, "consultation_routes")
        data = (
            copy.deepcopy(p["data"])
            if p
            else json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "docs/content/consultation-remainder-20261105.json"
                ).read_text()
            )["outside"]
        )
        for k in ("approval", "previous_version", "configuration_release"):
            data.pop(k, None)
        return data

    @app.get("/api/funnel-runs/<id>/consultation-journey")
    @protected()
    def details(id):
        from .funnel import consultation_promotion_eligible

        run = object_get(g.db, id, "funnel_run", g.actor["id"])
        j = journey_for(g.db, run)
        require(j, "Новый сценарий записи пока не опубликован", 404)
        s = run["data"]["snapshot"]
        eligible = consultation_promotion_eligible(g.db, run, g.now)
        if run["data"].get("booking_id"):
            booking = object_get(g.db, run["data"]["booking_id"], "booking", g.actor["id"])
            eligible = booking["data"].get("promotion", {}).get("eligible") is True
            j = booking["data"].get("journey_snapshot") or j
        j = presentation_journey(g.db, j, run["data"]["child_age"])
        return {
            "journey": j,
            "booking_id": run["data"].get("booking_id"),
            "eligible": eligible,
            "amount_rub": s["consultation_amount_rub"]
            if eligible
            else s["consultation_regular_rub"],
            "regular_rub": s["consultation_regular_rub"],
            "code": s["consultation_code"],
            "contact_url": s.get("contact_url", ""),
        }

    @app.get("/api/bookings/<id>/preparation")
    @protected()
    def preparation(id):
        booking = object_get(g.db, id, "booking", g.actor["id"])
        require(
            booking["data"].get("paid_at") is not None
            and booking["status"] != "cancelled"
            and booking["data"].get("payment_timing") == "before_scheduling",
            "Памятка пока недоступна",
            404,
        )
        j = booking["data"].get("journey_snapshot", {})
        blob = g.db.execute(
            "SELECT body FROM blobs WHERE id=? AND case_id=? AND mime='application/pdf'",
            (j.get("preparation_asset_id"), j.get("preparation_material_id")),
        ).fetchone()
        require(blob, "Материал недоступен", 404)
        audit(
            g.db,
            g.actor["id"],
            "booking.preparation-view",
            booking,
            g.now,
            "Получение памятки подготовки",
        )
        return Response(blob["body"], mimetype="application/pdf")

    @app.get("/api/consultation-routes")
    @protected()
    def routes():
        p = policies(g.db, "consultation_routes")
        require(p, "Маршруты другого возраста пока не опубликованы", 404)
        return {
            k: copy.deepcopy(p["data"][k])
            for k in ("menu_text", "routes", "contact_url", "specialist_name")
        }

    @app.post("/api/consultation-leads")
    @protected()
    def lead():
        d = body()
        p = policies(g.db, "consultation_routes")
        require(
            p and isinstance(d.get("route"), str) and d["route"] in p["data"]["routes"],
            "Выберите категорию обращения",
        )
        require(d.get("action") in ("contact", "booking", "download"), "Выберите действие")
        r = p["data"]["routes"][d["route"]]
        if d["route"] == "baby_0_2" and d["action"] == "booking":
            from .scope import require_service

            require_service(g.db, "video_task")
        if d["action"] == "download":
            require(material_ready(g.db, r.get("material_id")), "Материал ещё не опубликован", 404)
        payload = {
            "lead_type": d["route"],
            "action": d["action"],
            "amount_rub": r["amount_rub"],
            "label": r["label"],
            "policy_id": p["id"],
            "route_snapshot": copy.deepcopy(r),
            "contact_url": p["data"]["contact_url"],
        }

        def create():
            obj = insert(g.db, "consultation_lead", payload, g.now, g.actor["id"], status="new")
            emit(g.db, obj["owner"], obj["id"], "consultation.lead", g.now)
            audit(
                g.db,
                g.actor["id"],
                "consultation.lead",
                obj,
                g.now,
                "Запрошена личная связь со специалистом",
            )
            return obj

        return idempotent(
            g.db, "consultation-lead:" + g.actor["id"], d.get("key"), payload, create
        ), 201

    @app.get("/api/consultation-leads/<id>/pdf")
    @protected()
    def pdf(id):
        obj = object_get(g.db, id, "consultation_lead", g.actor["id"])
        require(obj["data"]["action"] == "download", "Запросите получение материала")
        mid = obj["data"]["route_snapshot"].get("material_id")
        material = object_get(g.db, mid, "material")
        blob = g.db.execute(
            "SELECT body FROM blobs WHERE id=? AND case_id=?",
            (material["data"].get("asset_id"), mid),
        ).fetchone()
        require(blob, "Материал недоступен", 404)
        return Response(blob["body"], mimetype="application/pdf")

    @app.post("/api/admin/consultation-leads/<id>/close")
    @protected(admin=True)
    def close(id):
        d = body()
        why = reason(d)
        obj = object_get(g.db, id, "consultation_lead")
        require(obj["revision"] == d.get("revision"), "Обращение изменено", 409)
        result = update(g.db, obj, g.now, status="closed")
        audit(g.db, g.actor["id"], "consultation.lead.close", result, g.now, why, obj["revision"])
        return result
