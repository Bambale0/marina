import hashlib
import json

from flask import g, render_template_string
from . import body, object_get, protected, reason, require
from .commerce import policies
from .db import audit, connect, insert, unpack, update


def marketing_allowed(db, actor):
    policy = policies(db, "privacy")
    if not policy or not policy["data"].get("require_marketing_consent"):
        return True
    row = db.execute(
        "SELECT data FROM objects WHERE kind='marketing_consent' AND owner=? "
        "ORDER BY created DESC,rowid DESC LIMIT 1",
        (actor,),
    ).fetchone()
    choice = json.loads(row["data"]) if row else {}
    return choice.get("accepted") is True and choice.get("policy_id") == policy["id"]


def offer_accepted(db, actor):
    offer = db.execute(
        "SELECT id FROM objects WHERE kind='template' AND status='published' "
        "AND json_extract(data,'$.type')='offer' ORDER BY updated DESC,rowid DESC LIMIT 1"
    ).fetchone()
    return bool(
        offer
        and db.execute(
            "SELECT 1 FROM objects WHERE kind='offer_acceptance' AND owner=? "
            "AND json_extract(data,'$.offer_id')=?",
            (actor, offer["id"]),
        ).fetchone()
    )


def consent_required(db, actor, allow_offer=False):
    if allow_offer and offer_accepted(db, actor):
        return
    p = policies(db, "privacy")
    require(
        p,
        "Условия обработки данных ещё готовятся. Ввод персональных данных пока закрыт.",
    )
    accepted = db.execute(
        "SELECT 1 FROM objects WHERE kind='consent' AND status='recorded' AND owner=? AND json_extract(data,'$.policy_id')=?",
        (actor, p["id"]),
    ).fetchone()
    require(accepted, "Сначала прочитайте и примите актуальные условия обработки данных")


def register_privacy(app):
    def public_policy():
        with connect(app.config["DATABASE"]) as db:
            p = policies(db, "privacy")
        require(p, "Политика обработки данных ещё готовится", 404)
        return {
            "id": p["id"],
            "title": p["data"].get("title", "Политика обработки данных"),
            "terms": p["data"]["terms"],
            "consent_text": p["data"].get("consent_text", ""),
            "version": p["revision"],
            "published_at": p["updated"],
        }

    @app.get("/api/privacy/current")
    def current_privacy():
        return public_policy()

    @app.get("/privacy")
    @app.get("/privacy/consent")
    def public_privacy_page():
        from flask import request

        p = public_policy()
        consent = request.path.endswith("/consent")
        title = "Согласие на обработку персональных данных" if consent else p["title"]
        text = p["consent_text"] if consent else p["terms"]
        return render_template_string(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>{{ title }}</title><link rel="stylesheet" href="/static/style.css">'
            '</head><body><main><h1>{{ title }}</h1><article class="readable">{{ text }}</article>'
            '<p><a href="/privacy">Политика</a> · '
            '<a href="/privacy/consent">Отдельное согласие</a></p></main></body></html>',
            title=title,
            text=text,
        )

    @app.get("/offer")
    def public_offer_page():
        with connect(app.config["DATABASE"]) as db:
            row = db.execute(
                "SELECT data FROM objects WHERE kind='template' AND status='published' "
                "AND json_extract(data,'$.type')='offer' ORDER BY updated DESC,rowid DESC LIMIT 1"
            ).fetchone()
        require(row, "Оферта ещё готовится", 404)
        data = json.loads(row["data"])
        return render_template_string(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>{{ title }}</title><link rel="stylesheet" href="/static/style.css">'
            '</head><body><main><h1>{{ title }}</h1><article class="readable">{{ text }}</article>'
            '<p><a href="/privacy">Политика обработки данных</a></p></main></body></html>',
            title=data["title"],
            text=data["text"],
        )

    @app.post("/api/marketing-consents")
    @protected()
    def marketing_consent():
        d = body()
        p = policies(g.db, "privacy")
        require(type(d.get("accepted")) is bool, "Выберите, получать ли предложения")
        require(p and d.get("policy_id") == p["id"], "Откройте актуальные условия")
        obj = insert(
            g.db,
            "marketing_consent",
            {
                "accepted": d["accepted"],
                "policy_id": p["id"],
                "at": g.now,
                "text": p["data"].get(
                    "marketing_consent_text", "Предложения и акции сервиса в Telegram"
                ),
            },
            g.now,
            g.actor["id"],
            status="active" if d["accepted"] else "revoked",
        )
        if not d["accepted"]:
            g.db.execute(
                "UPDATE outbox SET status='cancelled',leased_until=NULL WHERE owner=? "
                "AND status IN ('pending','processing') "
                "AND (event LIKE 'funnel.followup%' OR event LIKE 'funnel.refusal%')",
                (g.actor["id"],),
            )
        audit(
            g.db,
            g.actor["id"],
            "marketing.choice",
            obj,
            g.now,
            "Отдельный выбор рекламных сообщений",
        )
        return obj

    @app.post("/api/consents/revoke")
    @protected()
    def revoke_consent():
        body()
        for row in g.db.execute(
            "SELECT * FROM objects WHERE kind='consent' AND owner=? AND status='recorded'",
            (g.actor["id"],),
        ).fetchall():
            obj = unpack(row)
            result = update(g.db, obj, g.now, {**obj["data"], "revoked_at": g.now}, "revoked")
            audit(
                g.db, g.actor["id"], "consent.revoke", result, g.now, "Отзыв согласия пользователем"
            )
        obj = insert(
            g.db, "data_request", {"type": "withdrawal"}, g.now, g.actor["id"], status="requested"
        )
        p = policies(g.db, "privacy")
        insert(
            g.db,
            "marketing_consent",
            {"accepted": False, "policy_id": p["id"] if p else None, "at": g.now},
            g.now,
            g.actor["id"],
            status="revoked",
        )
        audit(
            g.db,
            g.actor["id"],
            "data.request",
            obj,
            g.now,
            "Отзыв согласия и проверка оснований хранения",
        )
        g.db.execute(
            "UPDATE outbox SET status='cancelled',leased_until=NULL WHERE owner=? "
            "AND status IN ('pending','processing') "
            "AND (event LIKE 'funnel.followup%' OR event LIKE 'funnel.refusal%')",
            (g.actor["id"],),
        )
        return {"ok": True, "request_id": obj["id"]}

    @app.post("/api/admin/media/<id>/delete")
    @protected(admin=True)
    def delete_media(id):
        d = body()
        why = reason(d)
        media = object_get(g.db, id, "media")
        require(
            d.get("revision") == media["revision"] and media["status"] == "ready",
            "Файл уже изменён",
            409,
        )
        case = object_get(g.db, media["data"].get("review_id") or media["data"].get("case_id"))
        require(
            case["status"] in ("closed", "completed")
            and media["data"].get("expires", g.now + 1) <= g.now,
            "Есть активное обязательство или срок хранения ещё не истёк",
        )
        g.db.execute("PRAGMA secure_delete=ON")
        g.db.execute("DELETE FROM blobs WHERE id=?", (id,))
        result = update(
            g.db,
            media,
            g.now,
            {**media["data"], "deleted_at": g.now, "deleted_by": g.actor["id"]},
            "deleted",
        )
        for row in g.db.execute(
            "SELECT * FROM objects WHERE kind='retention_task' AND json_extract(data,'$.media_id')=?",
            (id,),
        ).fetchall():
            update(g.db, unpack(row), g.now, status="completed")
        audit(g.db, g.actor["id"], "media.delete", result, g.now, why, media["revision"])
        return result

    @app.post("/api/admin/data-requests/<id>/<action>")
    @protected(admin=True)
    def data_request_action(id, action):
        d = body()
        why = reason(d)
        obj = object_get(g.db, id, "data_request")
        require(d.get("revision") == obj["revision"], "Запрос изменён", 409)
        require(
            action in ("in_progress", "awaiting_policy", "reject"),
            "Этот результат не подтверждает фактическое удаление или выгрузку",
            422,
        )
        result = update(
            g.db,
            obj,
            g.now,
            {**obj["data"], "operator_note": why},
            "rejected" if action == "reject" else action,
        )
        audit(g.db, g.actor["id"], "data." + action, result, g.now, why, obj["revision"])
        return result

    @app.get("/api/privacy")
    @protected()
    def privacy():
        p = policies(g.db, "privacy")
        accepted = bool(
            p
            and g.db.execute(
                "SELECT 1 FROM objects WHERE kind='consent' AND status='recorded' AND owner=? AND json_extract(data,'$.policy_id')=?",
                (g.actor["id"], p["id"]),
            ).fetchone()
        )
        return {"policy": p, "accepted": accepted}

    @app.post("/api/consents")
    @protected()
    def consent():
        d = body()
        p = policies(g.db, "privacy")
        require(
            p and d.get("policy_id") == p["id"] and d.get("accepted") is True,
            "Подтвердите актуальную редакцию условий",
        )
        require(
            not p["data"].get("require_representative_confirmation")
            or d.get("representative_confirmed") is True,
            "Подтвердите, что вы совершеннолетний участник или законный представитель ребёнка",
        )
        from .screening import idempotent

        revoked_count = g.db.execute(
            "SELECT count(*) FROM objects WHERE kind='consent' AND owner=? "
            "AND status='revoked' AND json_extract(data,'$.policy_id')=?",
            (g.actor["id"], p["id"]),
        ).fetchone()[0]
        consent_key = p["id"] + (":renewal:" + str(revoked_count) if revoked_count else "")
        return idempotent(
            g.db,
            "consent:" + g.actor["id"],
            consent_key,
            {"policy_id": p["id"]},
            lambda: insert(
                g.db,
                "consent",
                {
                    "policy_id": p["id"],
                    "accepted_at": g.now,
                    "policy_revision": p["revision"],
                    "policy_digest": hashlib.sha256(p["data"]["terms"].encode()).hexdigest(),
                    "consent_text_snapshot": p["data"].get("consent_text", p["data"]["terms"]),
                    "representative_confirmed": d.get("representative_confirmed") is True,
                },
                g.now,
                g.actor["id"],
                status="recorded",
            ),
        )

    @app.post("/api/data-requests")
    @protected()
    def data_request():
        d = body()
        require(d.get("type") in ("export", "delete"), "Выберите выгрузку или удаление")
        result = insert(
            g.db,
            "data_request",
            {"type": d["type"]},
            g.now,
            g.actor["id"],
            status="requested",
        )
        audit(g.db, g.actor["id"], "data.request", result, g.now, "Запрос пользователя")
        return result, 201

    @app.get("/api/data-export")
    @protected()
    def export_data():
        from .views import client_view

        return {
            "account": {k: v for k, v in g.actor.items() if k not in ("role", "blocked")},
            "records": [
                client_view(unpack(r))
                for r in g.db.execute(
                    "SELECT * FROM objects WHERE owner=? AND kind NOT IN ('bot_callback','bot_state','ai_job','reconciliation','correction')",
                    (g.actor["id"],),
                )
            ],
        }

    @app.post("/api/admin/subjects/<id>")
    @protected(admin=True)
    def edit_subject_admin(id):
        from . import age_months, Problem

        obj = object_get(g.db, id, "subject")
        d = body()
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Профиль изменён", 409)
        payload = {**obj["data"]}
        if "name" in d:
            require(isinstance(d["name"], str) and 1 <= len(d["name"]) <= 100, "Укажите имя")
            payload["name"] = d["name"]
        if "birth_date" in d:
            try:
                require(
                    0 <= age_months(d["birth_date"], g.now) <= 1440,
                    "Неверная дата рождения",
                )
            except (TypeError, ValueError):
                raise Problem("Неверная дата рождения")
            payload["birth_date"] = d["birth_date"]
        result = update(g.db, obj, g.now, payload, "archived" if d.get("archive") else None)
        audit(
            g.db,
            g.actor["id"],
            "subject.admin-edit",
            result,
            g.now,
            why,
            obj["revision"],
        )
        return result
