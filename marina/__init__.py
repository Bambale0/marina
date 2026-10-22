import functools
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .db import SCHEMA, audit, connect, insert, transaction, uid, unpack, update


class Problem(Exception):
    def __init__(self, message, status=422):
        self.message, self.status = message, status


def require(condition, message, status=422):
    if not condition:
        raise Problem(message, status)


def body():
    data = request.get_json(silent=True)
    require(isinstance(data, dict), "Ожидается JSON-объект")
    return data


def reason(data):
    value = data.get("reason", "")
    require(
        isinstance(value, str) and 3 <= len(value.strip()) <= 1000,
        "Укажите причину действия (3–1000 символов)",
    )
    return value.strip()


def object_get(db, id, kind=None, owner=None):
    require(isinstance(id, str) and 1 <= len(id) <= 200, "Объект не найден", 404)
    obj = unpack(db.execute("SELECT * FROM objects WHERE id=?", (id,)).fetchone())
    require(
        obj and (kind is None or obj["kind"] == kind) and (owner is None or obj["owner"] == owner),
        "Объект не найден",
        404,
    )
    return obj


def age_months(birth_date, now):
    born = date.fromisoformat(birth_date)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return (today.year - born.year) * 12 + today.month - born.month - (today.day < born.day)


def protected(admin=False):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            digest = hashlib.sha256(token.encode()).hexdigest()
            with transaction(g.app.config["DATABASE"]) as db:
                account = db.execute(
                    "SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.digest=? AND s.revoked=0 AND s.expires>?",
                    (digest, g.now),
                ).fetchone()
                require(account, "Войдите через Telegram", 401)
                g.actor = dict(account)
                require(not account["blocked"], "Доступ ограничен", 403)
                require(
                    not admin or account["role"] == "admin",
                    "Требуются права специалиста",
                    403,
                )
                g.db = db
                if (
                    g.app.config.get("ENFORCE_CONSENT", True)
                    and request.method not in ("GET", "HEAD")
                    and not request.path.startswith("/api/admin/")
                    and any(
                        request.path.startswith("/api/" + prefix)
                        for prefix in (
                            "subjects",
                            "video-reviews",
                            "attempts",
                            "bookings",
                            "quotes",
                            "cases",
                            "lessons",
                        )
                    )
                ):
                    from .privacy import consent_required

                    consent_required(db, account["id"])
                result = fn(*args, **kwargs)
                return result

        return wrapped

    return decorate


def create_app(config=None):
    if config is None:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    app = Flask(__name__, static_folder="static")
    app.config.update(
        DATABASE=os.getenv("DATABASE", "var/marina.sqlite"),
        BOT_TOKEN=os.getenv("BOT_TOKEN", ""),
        ADMIN_TELEGRAM_IDS={int(v) for v in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if v},
        CLOCK=time.time,
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        SESSION_TTL=12 * 3600,
        PUBLIC_URL=os.getenv("PUBLIC_URL", ""),
        TELEGRAM_WEBHOOK_SECRET=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        SALES_ENABLED=False,
    )
    if config:
        app.config.update(config)
    if os.getenv("TRUST_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with connect(app.config["DATABASE"]) as db:
        db.executescript(SCHEMA)

    @app.before_request
    def context():
        g.app, g.now = app, int(app.config["CLOCK"]())
        # Mutations only accept JSON or a file with an explicit Bearer token.
        if request.path.startswith("/api/") and request.method not in (
            "GET",
            "HEAD",
            "OPTIONS",
        ):
            require(
                not request.headers.get("Origin")
                or request.headers["Origin"] == request.host_url.rstrip("/"),
                "Недопустимый источник запроса",
                403,
            )

    @app.after_request
    def headers(response):
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org",
            }
        )
        if (
            response.is_json
            and request.path.startswith("/api/")
            and not request.path.startswith("/api/admin/")
        ):
            from .views import client_view

            def sanitize(value):
                if isinstance(value, dict):
                    if isinstance(value.get("data"), dict) and value.get("kind"):
                        value = client_view(value)
                    return {k: sanitize(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [sanitize(v) for v in value]
                return value

            response.set_data(app.json.dumps(sanitize(response.get_json())))
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(Problem)
    def problem(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.get("/")
    @app.get("/admin")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/health")
    def health():
        with connect(app.config["DATABASE"]) as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/api/auth/telegram")
    def auth():
        raw = body().get("init_data", "")
        require(isinstance(raw, str) and len(raw) <= 16384, "Неверные данные входа", 401)
        pairs = parse_qsl(raw, keep_blank_values=True)
        require(len(dict(pairs)) == len(pairs), "Повторяющиеся поля входа", 401)
        data = dict(pairs)
        signature = data.pop("hash", "")
        require(app.config["BOT_TOKEN"], "Telegram-вход ещё не настроен", 503)
        key = hmac.new(b"WebAppData", app.config["BOT_TOKEN"].encode(), hashlib.sha256).digest()
        expected = hmac.new(
            key,
            "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(),
            hashlib.sha256,
        ).hexdigest()
        require(hmac.compare_digest(signature, expected), "Неверная подпись Telegram", 401)
        try:
            auth_time = int(data["auth_date"])
            user = json.loads(data["user"])
            tg_id = user["id"]
            require(type(tg_id) is int and tg_id > 0, "Неверный пользователь", 401)
        except (KeyError, ValueError, TypeError):
            raise Problem("Неверные данные Telegram", 401)
        require(-30 <= g.now - auth_time <= 300, "Время авторизации истекло", 401)
        with transaction(app.config["DATABASE"]) as db:
            replay = hashlib.sha256(raw.encode()).hexdigest()
            require(
                not db.execute("SELECT 1 FROM auth_uses WHERE digest=?", (replay,)).fetchone(),
                "Запрос входа уже использован. Откройте приложение заново",
                401,
            )
            row = db.execute("SELECT * FROM accounts WHERE telegram_id=?", (tg_id,)).fetchone()
            if row is None:
                id = uid()
                db.execute(
                    "INSERT INTO accounts VALUES(?,?,?,?,0,?)",
                    (
                        id,
                        tg_id,
                        str(user.get("first_name", ""))[:120],
                        "admin" if tg_id in app.config["ADMIN_TELEGRAM_IDS"] else "client",
                        g.now,
                    ),
                )
                row = db.execute("SELECT * FROM accounts WHERE id=?", (id,)).fetchone()
            require(not row["blocked"], "Доступ ограничен", 403)
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    row["id"],
                    g.now + app.config["SESSION_TTL"],
                ),
            )
            wanted_role = "admin" if tg_id in app.config["ADMIN_TELEGRAM_IDS"] else "client"
            if row["role"] != wanted_role:
                db.execute("UPDATE accounts SET role=? WHERE id=?", (wanted_role, row["id"]))
                row = db.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
            db.execute("INSERT INTO auth_uses VALUES(?,?)", (replay, g.now + 360))
            db.execute("DELETE FROM auth_uses WHERE expires<?", (g.now,))
            return {
                "token": token,
                "expires_at": g.now + app.config["SESSION_TTL"],
                "account": dict(row),
            }

    @app.post("/api/auth/code")
    def login_code():
        code = body().get("code", "")
        require(isinstance(code, str) and 20 <= len(code) <= 100, "Неверный код входа", 401)
        digest = hashlib.sha256(code.encode()).hexdigest()
        with transaction(app.config["DATABASE"]) as db:
            row = db.execute(
                "SELECT * FROM login_codes WHERE digest=? AND expires>?",
                (digest, g.now),
            ).fetchone()
            require(row, "Код истёк или уже использован", 401)
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            require(account and not account["blocked"], "Доступ ограничен", 403)
            db.execute("DELETE FROM login_codes WHERE digest=?", (digest,))
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    account["id"],
                    g.now + app.config["SESSION_TTL"],
                ),
            )
            return {"token": token, "account": dict(account)}

    @app.post("/api/logout")
    @protected()
    def logout():
        digest = hashlib.sha256(
            request.headers["Authorization"].removeprefix("Bearer ").encode()
        ).hexdigest()
        g.db.execute("UPDATE sessions SET revoked=1 WHERE digest=?", (digest,))
        return {"ok": True}

    @app.get("/api/me")
    @protected()
    def me():
        subjects = [
            unpack(r)
            for r in g.db.execute(
                "SELECT * FROM objects WHERE kind='subject' AND owner=? ORDER BY created,id",
                (g.actor["id"],),
            )
        ]
        trial = g.db.execute(
            "SELECT starts,ends FROM trials WHERE account_id=?", (g.actor["id"],)
        ).fetchone()
        return {
            "account": g.actor,
            "subjects": subjects,
            "trial": dict(trial) if trial else None,
            "now": g.now,
        }

    @app.post("/api/subjects")
    @protected()
    def create_subject():
        data = body()
        require(data.get("kind") in ("adult", "child"), "Выберите тип профиля")
        require(
            isinstance(data.get("name"), str) and 1 <= len(data["name"].strip()) <= 100,
            "Укажите имя",
        )
        try:
            months = age_months(data.get("birth_date", ""), g.now)
        except (TypeError, ValueError):
            raise Problem("Неверная дата рождения")
        require(0 <= months <= 1440, "Неверная дата рождения")
        payload = {k: data[k] for k in ("kind", "name", "birth_date")}

        def create():
            obj = insert(g.db, "subject", payload, g.now, g.actor["id"], status="active")
            audit(g.db, g.actor["id"], "subject.create", obj, g.now, "Создание своего профиля")
            return obj

        if data.get("key"):
            from .screening import idempotent

            obj = idempotent(g.db, "subject:" + g.actor["id"], data["key"], payload, create)
        else:
            obj = create()
        return {**obj, **obj["data"]}, 201

    @app.get("/api/subjects/<id>")
    @protected()
    def get_subject(id):
        return object_get(g.db, id, "subject", g.actor["id"])

    @app.patch("/api/subjects/<id>")
    @protected()
    def edit_subject(id):
        obj = object_get(g.db, id, "subject", g.actor["id"])
        data = body()
        require(
            data.get("revision") == obj["revision"],
            "Профиль изменён, обновите страницу",
            409,
        )
        payload = {**obj["data"]}
        if "name" in data:
            require(
                isinstance(data["name"], str) and 1 <= len(data["name"].strip()) <= 100,
                "Укажите имя",
            )
            payload["name"] = data["name"].strip()
        if "birth_date" in data:
            try:
                require(
                    0 <= age_months(data["birth_date"], g.now) <= 1440,
                    "Неверная дата рождения",
                )
            except (TypeError, ValueError):
                raise Problem("Неверная дата рождения")
            payload["birth_date"] = data["birth_date"]
        result = update(g.db, obj, g.now, payload, "archived" if data.get("archive") else None)
        audit(
            g.db,
            g.actor["id"],
            "subject.edit",
            result,
            g.now,
            "Изменение своего профиля",
            obj["revision"],
        )
        return result

    @app.get("/api/admin/accounts")
    @protected(admin=True)
    def accounts():
        term = "%" + request.args.get("q", "")[:100] + "%"
        return {
            "items": [
                dict(r)
                for r in g.db.execute(
                    "SELECT * FROM accounts WHERE name LIKE ? OR id LIKE ? ORDER BY created LIMIT 100 OFFSET ?",
                    (term, term, offset()),
                )
            ]
        }

    @app.post("/api/admin/accounts/<id>/<action>")
    @protected(admin=True)
    def account_action(id, action):
        why = reason(body())
        require(
            action in ("block", "restore", "revoke-sessions"),
            "Неизвестное действие",
            404,
        )
        require(
            g.db.execute("SELECT 1 FROM accounts WHERE id=?", (id,)).fetchone(),
            "Аккаунт не найден",
            404,
        )
        require(
            id != g.actor["id"] or action == "revoke-sessions",
            "Нельзя ограничить собственный вход",
        )
        if action == "revoke-sessions":
            g.db.execute("UPDATE sessions SET revoked=1 WHERE account_id=?", (id,))
        else:
            g.db.execute("UPDATE accounts SET blocked=? WHERE id=?", (int(action == "block"), id))
        audit(g.db, g.actor["id"], "account." + action, {"id": id}, g.now, why)
        return {"ok": True}

    @app.get("/api/admin/accounts/<id>")
    @protected(admin=True)
    def account_card(id):
        account = g.db.execute("SELECT * FROM accounts WHERE id=?", (id,)).fetchone()
        require(account, "Аккаунт не найден", 404)
        trial = g.db.execute("SELECT starts,ends FROM trials WHERE account_id=?", (id,)).fetchone()
        state = unpack(
            g.db.execute(
                "SELECT * FROM objects WHERE kind='bot_state' AND owner=?", (id,)
            ).fetchone()
        )
        introduction = None
        if state and state["data"].get("concern"):
            introduction = {
                "age_group": state["data"].get("age_group"),
                "concern": state["data"]["concern"],
            }
        return {
            "account": dict(account),
            "introduction": introduction,
            "trial": dict(trial) if trial else None,
            "objects": [
                unpack(r)
                for r in g.db.execute(
                    "SELECT * FROM objects WHERE owner=? AND kind NOT IN ('bot_state','bot_callback') ORDER BY created DESC LIMIT 200",
                    (id,),
                )
            ],
            "sessions": [
                dict(r)
                for r in g.db.execute(
                    "SELECT expires,revoked FROM sessions WHERE account_id=?", (id,)
                )
            ],
        }

    @app.get("/api/admin/audit")
    @protected(admin=True)
    def audit_list():
        id = request.args.get("object_id")
        rows = g.db.execute(
            "SELECT * FROM audit WHERE (? IS NULL OR object_id=?) ORDER BY id LIMIT 100 OFFSET ?",
            (id, id, offset()),
        )
        return {"items": [dict(r) for r in rows]}

    from .content import register_content

    register_content(app)
    from .screening import register_screening

    register_screening(app)
    from .commerce import register_commerce

    register_commerce(app)
    from .services import register_services

    register_services(app)
    from .crm import register_crm

    register_crm(app)
    from .training import register_training

    register_training(app)
    from .providers import register_providers
    from .operations import register_operations

    register_providers(app)
    register_operations(app)
    from .privacy import register_privacy

    register_privacy(app)
    from .ai import register_ai

    register_ai(app)
    from .refunds import register_refunds

    register_refunds(app)
    from .attachments import register_attachments

    register_attachments(app)
    from .analytics import register_analytics

    register_analytics(app)
    return app


def offset():
    try:
        return max(0, int(request.args.get("offset", "0")))
    except ValueError:
        raise Problem("Неверная страница")
