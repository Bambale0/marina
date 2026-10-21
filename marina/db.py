"""SQLite persistence. Each command runs in one write transaction."""

import json
import sqlite3
import uuid
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'client', blocked INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT REFERENCES accounts(id), subject TEXT, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS object_versions(object_id TEXT NOT NULL REFERENCES objects(id),revision INTEGER NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,at INTEGER NOT NULL,PRIMARY KEY(object_id,revision));
INSERT OR IGNORE INTO object_versions SELECT id,revision,status,data,updated FROM objects;
CREATE INDEX IF NOT EXISTS objects_lookup ON objects(kind, owner, status);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT NOT NULL, at INTEGER NOT NULL, reason TEXT NOT NULL, before_revision INTEGER, after_revision INTEGER, correlation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dedup(scope TEXT NOT NULL, key TEXT NOT NULL, object_id TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS receipts(provider TEXT NOT NULL, charge TEXT NOT NULL, order_id TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, paid_at INTEGER NOT NULL, received_at INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY(provider,charge));
CREATE TABLE IF NOT EXISTS intent_fulfillments(intent_id TEXT NOT NULL, subject_id TEXT NOT NULL, order_id TEXT NOT NULL, object_id TEXT NOT NULL, PRIMARY KEY(intent_id,subject_id));
CREATE TABLE IF NOT EXISTS fulfillments(order_id TEXT NOT NULL, line_id TEXT NOT NULL, object_id TEXT NOT NULL, PRIMARY KEY(order_id,line_id));
CREATE TABLE IF NOT EXISTS trials(account_id TEXT PRIMARY KEY REFERENCES accounts(id), starts INTEGER NOT NULL, ends INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, owner TEXT NOT NULL, object_id TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, available INTEGER NOT NULL, leased_until INTEGER, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL REFERENCES outbox(id), at INTEGER NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, subject TEXT NOT NULL, case_id TEXT NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, body BLOB NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_codes(digest TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS auth_uses(digest TEXT PRIMARY KEY, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path):
    db = sqlite3.connect(path, timeout=30, isolation_level=None, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


@contextmanager
def transaction(path):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def uid():
    return uuid.uuid4().hex


def unpack(row):
    if row is None:
        return None
    obj = dict(row)
    if "data" in obj:
        obj["data"] = json.loads(obj["data"])
    return obj


def insert(db, kind, data, now, owner=None, subject=None, status="draft", id=None):
    id = id or uid()
    db.execute(
        "INSERT INTO objects VALUES(?,?,?,?,?,1,?,?,?)",
        (
            id,
            kind,
            owner,
            subject,
            status,
            json.dumps(data, ensure_ascii=False),
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO object_versions VALUES(?,1,?,?,?)",
        (id, status, json.dumps(data, ensure_ascii=False), now),
    )
    return unpack(db.execute("SELECT * FROM objects WHERE id=?", (id,)).fetchone())


def update(db, obj, now, data=None, status=None):
    db.execute(
        "INSERT OR IGNORE INTO object_versions VALUES(?,?,?,?,?)",
        (
            obj["id"],
            obj["revision"],
            obj["status"],
            json.dumps(obj["data"], ensure_ascii=False),
            obj["updated"],
        ),
    )
    db.execute(
        "UPDATE objects SET data=?,status=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
        (
            json.dumps(obj["data"] if data is None else data, ensure_ascii=False),
            status or obj["status"],
            now,
            obj["id"],
            obj["revision"],
        ),
    )
    result = unpack(db.execute("SELECT * FROM objects WHERE id=?", (obj["id"],)).fetchone())
    db.execute(
        "INSERT OR IGNORE INTO object_versions VALUES(?,?,?,?,?)",
        (
            result["id"],
            result["revision"],
            result["status"],
            json.dumps(result["data"], ensure_ascii=False),
            now,
        ),
    )
    return result


def audit(db, actor, action, obj, now, reason, before=None):
    db.execute(
        "INSERT INTO audit(actor,action,object_id,at,reason,before_revision,after_revision,correlation) VALUES(?,?,?,?,?,?,?,?)",
        (actor, action, obj["id"], now, reason, before, obj.get("revision"), uid()),
    )


def emit(db, owner, obj, event, now):
    db.execute(
        "INSERT INTO outbox(id,owner,object_id,event,available,created) VALUES(?,?,?,?,?,?)",
        (uid(), owner, obj, event, now, now),
    )
