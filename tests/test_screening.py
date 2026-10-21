from tests.test_identity import app, login


def publish(c, kind, data):
    obj = c.post(
        "/api/admin/content",
        json={
            "kind": kind,
            "title": "Test fixture only",
            "data": data,
            "reason": "Test fixture",
        },
    ).json
    assert "id" in obj, obj
    obj = c.post(
        f"/api/admin/content/{obj['id']}/approve",
        json={"revision": obj["revision"], "reason": "Expert approval"},
    ).json
    assert "id" in obj, obj
    obj = c.post(
        f"/api/admin/content/{obj['id']}/publish",
        json={"revision": obj["revision"], "reason": "Publish tested version"},
    ).json
    assert obj.get("status") == "published", obj
    return obj


def method():
    return {
        "audience": "adult",
        "min_months": 216,
        "max_months": 1440,
        "source": "SYNTHETIC TEST, NOT A CLINICAL TOOL",
        "rights": "test",
        "limitations": "No diagnosis",
        "questions": [
            {
                "id": "q1",
                "text": "Question",
                "required": True,
                "options": [
                    {"id": "a", "text": "A", "score": 0},
                    {"id": "b", "text": "B", "score": 2},
                ],
            }
        ],
        "bands": [
            {"min": 0, "max": 1, "text": "Low"},
            {"min": 2, "max": 2, "text": "High"},
        ],
        "examples": [
            {"answers": {"q1": "a"}, "score": 0, "text": "Low"},
            {"answers": {"q1": "b"}, "score": 2, "text": "High"},
        ],
    }


def subject(c, kind="adult"):
    return c.post(
        "/api/subjects",
        json={
            "kind": kind,
            "name": "Fixture",
            "birth_date": "1990-01-01" if kind == "adult" else "2020-01-01",
        },
    ).json


def test_resume_result_and_version_isolation(app):
    admin, owner, stranger = login(app), login(app, 2), login(app, 3)
    m = publish(admin, "method", method())
    s = subject(owner)
    start = {"method_id": m["id"], "subject_id": s["id"], "key": "one"}
    attempt = owner.post("/api/attempts", json=start).json
    assert owner.post("/api/attempts", json=start).json["id"] == attempt["id"]
    assert stranger.get("/api/attempts/" + attempt["id"]).status_code == 404
    assert (
        owner.post(f"/api/attempts/{attempt['id']}/complete", json={"revision": 1}).status_code
        == 422
    )
    saved = owner.patch(
        "/api/attempts/" + attempt["id"], json={"revision": 1, "answers": {"q1": "b"}}
    ).json
    assert (
        owner.patch(
            "/api/attempts/" + attempt["id"],
            json={"revision": 1, "answers": {"q1": "a"}},
        ).status_code
        == 409
    )
    admin.post(
        f"/api/admin/content/{m['id']}/archive",
        json={"revision": m["revision"], "reason": "Replaced version"},
    )
    result = owner.post(
        f"/api/attempts/{attempt['id']}/complete", json={"revision": saved["revision"]}
    ).json
    assert result["data"]["result"]["score"] == 2
    assert result["data"]["result"]["text"] == "High"
    assert result["data"]["method_id"] == m["id"]
    assert owner.post("/api/attempts", json={**start, "key": "new"}).status_code == 422


def test_age_and_unapproved_method(app):
    admin, c = login(app), login(app, 2)
    m = publish(admin, "method", method())
    child = subject(c, "child")
    assert (
        c.post(
            "/api/attempts",
            json={"method_id": m["id"], "subject_id": child["id"], "key": "k"},
        ).status_code
        == 422
    )
    bad = method()
    bad["examples"][0]["score"] = 999
    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "method",
            "title": "Invalid",
            "data": bad,
            "reason": "Test invalid",
        },
    ).json
    assert (
        admin.post(
            f"/api/admin/content/{draft['id']}/approve",
            json={"revision": 1, "reason": "Testing validation"},
        ).status_code
        == 422
    )


def test_new_publication_retires_old_starts_but_not_results(app):
    a, c = login(app), login(app, 2)
    first = publish(a, "method", method())
    copied = a.post(
        "/api/admin/content/" + first["id"] + "/copy",
        json={"revision": first["revision"], "reason": "Prepare new version"},
    ).json
    approved = a.post(
        "/api/admin/content/" + copied["id"] + "/approve",
        json={"revision": copied["revision"], "reason": "Check new version"},
    ).json
    second = a.post(
        "/api/admin/content/" + approved["id"] + "/publish",
        json={"revision": approved["revision"], "reason": "Release new version"},
    ).json
    ids = [m["id"] for m in c.get("/api/catalog/method").json["items"]]
    assert ids == [second["id"]]
    assert a.get("/api/admin/content/" + first["id"]).json["status"] == "archived"
