"""Session durability, Redis outages, and revocation through public APIs."""

import hashlib

import redis

from marina import create_app
from marina.bot import Bot
from marina.db import connect
from tests.test_identity import app, login, signed


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.unavailable = False

    def get(self, key):
        if self.unavailable:
            raise redis.ConnectionError("Synthetic outage")
        return self.values.get(key)

    def set(self, key, value, ex=None):
        if self.unavailable:
            raise redis.ConnectionError("Synthetic outage")
        self.values[key] = value

    def delete(self, key):
        if self.unavailable:
            raise redis.ConnectionError("Synthetic outage")
        self.values.pop(key, None)


def session(app, uid=2):
    backend = MemoryRedis()
    app.extensions["session_redis"] = backend
    client = app.test_client()
    response = client.post("/api/auth/telegram", json={"init_data": signed(uid, app.clock[0])})
    assert response.status_code == 200, response.json
    token = response.json["token"]
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + token
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert backend.values, "Login must persist the session in Redis"
    return backend, client, token, digest


def test_session_survives_app_restart_redis_loss_and_recovery(app):
    backend, client, token, digest = session(app)
    owner = client.get("/api/me").json["account"]["id"]
    restarted = create_app(dict(app.config))
    restarted.extensions["session_redis"] = backend
    restored = restarted.test_client()
    restored.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + token
    assert restored.get("/api/me").json["account"]["id"] == owner
    backend.values.clear()
    assert restored.get("/api/me").status_code == 200
    assert backend.values
    backend.unavailable = True
    assert restored.get("/api/me").status_code == 200
    backend.unavailable = False
    assert restored.get("/api/me").status_code == 200
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT account_id FROM sessions WHERE digest=?", (digest,)).fetchone()[0]
            == owner
        )


def test_logout_and_admin_revoke_reject_stale_redis_sessions(app):
    backend, client, token, _ = session(app)
    stale = dict(backend.values)
    assert client.post("/api/logout", json={}).status_code == 200
    backend.values = stale
    assert client.get("/api/me").status_code == 401

    app.clock[0] += 1
    backend, client, token, _ = session(app)
    owner = client.get("/api/me").json["account"]["id"]
    admin = login(app)
    backend.unavailable = True
    assert (
        admin.post(
            f"/api/admin/accounts/{owner}/revoke-sessions", json={"reason": "Requested revocation"}
        ).status_code
        == 200
    )
    backend.unavailable = False
    assert client.get("/api/me").status_code == 401


def test_expired_session_and_blocked_account_cannot_use_redis(app):
    backend, client, _, _ = session(app)
    owner = client.get("/api/me").json["account"]["id"]
    admin = login(app)
    assert (
        admin.post(
            f"/api/admin/accounts/{owner}/block", json={"reason": "Requested account block"}
        ).status_code
        == 200
    )
    assert client.get("/api/me").status_code == 403
    assert (
        admin.post(
            f"/api/admin/accounts/{owner}/restore", json={"reason": "Requested account restore"}
        ).status_code
        == 200
    )
    app.clock[0] += app.config["SESSION_TTL"]
    assert client.get("/api/me").status_code == 401


def test_bot_dialogue_and_callbacks_survive_app_restart_and_redis_outage(app):
    backend = MemoryRedis()
    app.extensions["session_redis"] = backend
    bot = Bot(app, {"id": 2}, 2, "before")
    bot.state_set(step="fixture-pending", age_group="3-4")
    keyboard = bot.keyboard([("Continue", "funnel_resume", {"id": "fixture"})])
    restarted = create_app(dict(app.config))
    backend.unavailable = True
    restarted.extensions["session_redis"] = backend
    restored = Bot(restarted, {"id": 2}, 2, "after")
    assert restored.state["id"] == bot.state["id"]
    assert restored.state["data"]["step"] == "fixture-pending"
    callback_id = keyboard["inline_keyboard"][0][0]["callback_data"][2:]
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute("SELECT owner FROM objects WHERE id=?", (callback_id,)).fetchone()[0]
            == restored.owner
        )
