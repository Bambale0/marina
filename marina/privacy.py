from flask import g
from . import body, object_get, protected, reason, require
from .commerce import policies
from .db import audit, insert, unpack, update


def consent_required(db, actor):
    p = policies(db, "privacy")
    require(
        p,
        "Условия обработки данных ещё готовятся. Ввод персональных данных пока закрыт.",
    )
    accepted = db.execute(
        "SELECT 1 FROM objects WHERE kind='consent' AND owner=? AND json_extract(data,'$.policy_id')=?",
        (actor, p["id"]),
    ).fetchone()
    require(accepted, "Сначала прочитайте и примите актуальные условия обработки данных")


def register_privacy(app):
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
                "SELECT 1 FROM objects WHERE kind='consent' AND owner=? AND json_extract(data,'$.policy_id')=?",
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
        from .screening import idempotent

        return idempotent(
            g.db,
            "consent:" + g.actor["id"],
            p["id"],
            {"policy_id": p["id"]},
            lambda: insert(
                g.db,
                "consent",
                {"policy_id": p["id"], "accepted_at": g.now},
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
