import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from marina import create_app


def signed(uid, now=1800000000, **extra):
    data = {
        "user": json.dumps({"id": uid, "first_name": "User"}),
        "auth_date": str(now),
        **extra,
    }
    key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
    data["hash"] = hmac.new(
        key,
        "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(data)


@pytest.fixture
def app(tmp_path):
    clock = [1800000000]
    app = create_app(
        {
            "TESTING": True,
            "ENFORCE_CONSENT": False,
            "SESSION_TTL": 30 * 24 * 3600,
            "DATABASE": str(tmp_path / "test.sqlite"),
            "BOT_TOKEN": "test-token",
            "ADMIN_TELEGRAM_IDS": {1},
            "CLOCK": lambda: clock[0],
        }
    )
    app.clock = clock
    return app


def login(app, uid=1):
    client = app.test_client()
    res = client.post("/api/auth/telegram", json={"init_data": signed(uid, app.clock[0])})
    assert res.status_code == 200, res.json
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + res.json["token"]
    return client


def test_tampered_and_stale_login(app):
    c = app.test_client()
    assert (
        c.post(
            "/api/auth/telegram", json={"init_data": signed(2).replace("User", "Evil")}
        ).status_code
        == 401
    )
    assert (
        c.post("/api/auth/telegram", json={"init_data": signed(2, app.clock[0] - 301)}).status_code
        == 401
    )
    assert (
        c.post("/api/auth/telegram", json={"init_data": signed(2, app.clock[0] + 31)}).status_code
        == 401
    )


def test_adult_child_isolation_and_block(app):
    owner, stranger, admin = login(app, 2), login(app, 3), login(app)
    assert owner.get("/api/me").json["subjects"] == []
    adult = owner.post(
        "/api/subjects",
        json={"kind": "adult", "name": "Parent", "birth_date": "1990-02-20"},
    ).json
    child = owner.post(
        "/api/subjects",
        json={"kind": "child", "name": "Child", "birth_date": "2020-01-01"},
    ).json
    assert stranger.get("/api/subjects/" + child["id"]).status_code == 404
    assert owner.get("/api/admin/accounts").status_code == 403
    assert adult["kind"] == "adult"
    aid = owner.get("/api/me").json["account"]["id"]
    assert (
        admin.post(
            "/api/admin/accounts/" + aid + "/block", json={"reason": "Requested"}
        ).status_code
        == 200
    )
    assert owner.get("/api/me").status_code == 403
    assert admin.get("/api/admin/audit").json["items"][-1]["reason"] == "Requested"


def test_registry_requires_reason_and_preserves_history(app):
    c = login(app)
    data = {
        "kind": "decision",
        "title": "First method",
        "data": {"owner": "specialist", "status": "open", "epics": ["E02"]},
    }
    assert c.post("/api/admin/content", json=data).status_code == 422
    created = c.post("/api/admin/content", json={**data, "reason": "Inventory"}).json
    assert created["status"] == "draft"
    assert c.get("/api/admin/content?kind=decision").json["items"]
    assert (
        c.patch(
            "/api/admin/content/" + created["id"],
            json={"revision": 0, "title": "oops", "reason": "Test"},
        ).status_code
        == 409
    )


def test_repeated_profile_command_is_idempotent(app):
    c = login(app, 2)
    data = {"kind": "adult", "name": "Fixture", "birth_date": "1990-01-01", "key": "same-update"}
    first = c.post("/api/subjects", json=data).json
    second = c.post("/api/subjects", json=data).json
    assert first["id"] == second["id"]
    assert len(c.get("/api/me").json["subjects"]) == 1
