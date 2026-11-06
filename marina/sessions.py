"""Redis session mirror with a durable SQLite registry and immediate revocation.

Redis never stores raw bearer tokens. The registry remains authoritative so an
outage, an old AOF snapshot, or a failed invalidation cannot restore revoked access.
"""

import hashlib
import json
import secrets

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry


def configure(app):
    url = app.config.get("REDIS_URL")
    app.extensions["session_redis"] = (
        redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
        )
        if url
        else None
    )


def mirror(app, digest, account_id, expires, now):
    backend = app.extensions.get("session_redis")
    if backend is None:
        return
    key = "marina:session:" + digest
    try:
        if expires <= now:
            backend.delete(key)
            return
        value = json.dumps({"account_id": account_id, "expires": expires}, sort_keys=True)
        if backend.get(key) != value:
            backend.set(key, value, ex=expires - now)
    except redis.RedisError:
        # SQL is committed independently; Redis availability must not log users out.
        pass


def forget(app, digest):
    backend = app.extensions.get("session_redis")
    if backend is not None:
        try:
            backend.delete("marina:session:" + digest)
        except redis.RedisError:
            pass


def issue(db, app, account_id, now, ttl):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    db.execute("INSERT INTO sessions VALUES(?,?,?,0)", (digest, account_id, now + ttl))
    mirror(app, digest, account_id, now + ttl, now)
    return token


def account(db, app, digest, now):
    session = db.execute(
        "SELECT * FROM sessions WHERE digest=? AND revoked=0 AND expires>?", (digest, now)
    ).fetchone()
    if session is None:
        forget(app, digest)
        return None
    mirror(app, digest, session["account_id"], session["expires"], now)
    return db.execute("SELECT * FROM accounts WHERE id=?", (session["account_id"],)).fetchone()
