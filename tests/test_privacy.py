from tests.test_identity import app, login
from tests.test_screening import publish


def test_consent_is_specific_to_current_published_version(app):
    app.config["ENFORCE_CONSENT"] = True
    a, c = login(app), login(app, 2)
    payload = {"kind": "adult", "name": "Fixture", "birth_date": "1990-01-01"}
    assert c.post("/api/subjects", json=payload).status_code == 422
    policy = publish(
        a,
        "policy",
        {
            "type": "privacy",
            "terms": "Version A",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    assert c.post("/api/subjects", json=payload).status_code == 422
    assert (
        c.post("/api/consents", json={"policy_id": policy["id"], "accepted": True}).status_code
        == 200
    )
    assert c.post("/api/subjects", json=payload).status_code == 201
    publish(
        a,
        "policy",
        {
            "type": "privacy",
            "terms": "Version B",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    assert c.post("/api/subjects", json=payload).status_code == 422


def test_session_expiry_logout_and_origin(app):
    c = login(app, 2)
    assert (
        c.post(
            "/api/subjects",
            json={"kind": "adult"},
            headers={"Origin": "https://foreign.example"},
        ).status_code
        == 403
    )
    app.clock[0] += 30 * 24 * 3600
    assert c.get("/api/me").status_code == 401
