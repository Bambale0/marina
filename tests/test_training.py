from concurrent.futures import ThreadPoolExecutor

from marina.db import transaction, insert
from tests.test_identity import app, login
from tests.test_screening import publish, subject


def exercises(app, a):
    with transaction(app.config["DATABASE"]) as db:
        # Media verification is the external boundary, not the behavior under test.
        db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                "video",
                "admin",
                "",
                "catalog",
                "video/mp4",
                "test",
                b"test-video",
                app.clock[0],
            ),
        )
    for n in range(2):
        publish(
            a,
            "exercise",
            {
                "source": "Synthetic fixture",
                "rights": "test",
                "instructions": "Exercise fixture",
                "duration_seconds": 360,
                "min_months": 36,
                "max_months": 132,
                "equipment": [],
                "exclusions": [],
                "difficulty": 1,
                "video_id": "video",
                "video_verified": True,
            },
        )
    publish(
        a,
        "selection",
        {
            "source": "Synthetic fixture",
            "instructions": "Fixed safe sequence",
            "min_seconds": 600,
            "max_seconds": 900,
            "difficulty": 1,
            "examples": [{"min_months": 36, "max_months": 132}],
        },
    )
    publish(
        a,
        "policy",
        {
            "type": "training",
            "terms": "Fixture calendar",
            "timezone": "Europe/Moscow",
            "expiry": "block",
            "trial_hours": 72,
        },
    )


def test_trial_only_on_ready_launch_and_account_global(app):
    a, c = login(app), login(app, 2)
    s1, s2 = subject(c, "child"), subject(c, "child")
    assert c.get("/api/me").json["trial"] is None
    assert (
        c.post(
            "/api/lessons/start",
            json={
                "subject_id": s1["id"],
                "equipment": [],
                "constraints": [],
                "key": "not-ready",
            },
        ).status_code
        == 422
    )
    assert c.get("/api/me").json["trial"] is None
    exercises(app, a)

    def launch(s):
        local = app.test_client()
        local.environ_base.update(c.environ_base)
        return local.post(
            "/api/lessons/start",
            json={
                "subject_id": s["id"],
                "equipment": [],
                "constraints": [],
                "key": s["id"],
            },
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(launch, [s1, s2]))
    assert all(r.status_code == 200 for r in results), [r.json for r in results]
    trial = c.get("/api/me").json["trial"]
    assert trial["ends"] - trial["starts"] == 72 * 3600
    lesson = results[0].json
    for _ in range(2):
        done = c.post(
            "/api/lessons/" + lesson["id"] + "/complete",
            json={
                "completed_exercises": [e["id"] for e in lesson["data"]["exercises"]],
                "feedback": "easy",
            },
        )
        assert done.status_code == 200, done.json
    app.clock[0] += 72 * 3600
    new = subject(c, "child")
    assert (
        c.post(
            "/api/lessons/start",
            json={
                "subject_id": new["id"],
                "equipment": [],
                "constraints": [],
                "key": "new",
            },
        ).status_code
        == 403
    )
    assert c.get("/api/me").json["trial"] == trial


def test_course_content_requires_own_right_and_prerequisites(app):
    a, c, other = login(app), login(app, 2), login(app, 3)
    s = subject(c)
    course = publish(
        a,
        "course",
        {
            "source": "Synthetic fixture",
            "instructions": "Program",
            "rights": "test",
            "audience": "adult",
            "min_months": 216,
            "max_months": 1440,
            "lessons": [
                {"id": "l1", "title": "First", "text": "Paid text"},
                {
                    "id": "l2",
                    "title": "Second",
                    "text": "Second paid text",
                    "requires": ["l1"],
                },
            ],
        },
    )
    assert "Paid text" not in str(c.get("/api/catalog/course").json)
    assert (
        c.get("/api/courses/" + course["id"] + "/lessons/l1?subject_id=" + s["id"]).status_code
        == 403
    )
    owner_id = c.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        insert(
            db,
            "right",
            {
                "service": "course",
                "starts": app.clock[0],
                "ends": app.clock[0] + 3600,
                "course_id": course["id"],
            },
            app.clock[0],
            owner_id,
            s["id"],
            "active",
        )
    path = "/api/courses/" + course["id"] + "/lessons/"
    assert c.get(path + "l2?subject_id=" + s["id"]).status_code == 403
    assert c.get(path + "l1?subject_id=" + s["id"]).json["text"] == "Paid text"
    assert c.post(path + "l1/complete", json={"subject_id": s["id"]}).status_code == 200
    assert c.get(path + "l2?subject_id=" + s["id"]).status_code == 200
    assert other.get(path + "l1?subject_id=" + s["id"]).status_code == 404


def test_prepared_plan_does_not_start_trial_and_replacement_is_checked(app):
    a, c = login(app), login(app, 2)
    s = subject(c, "child")
    exercises(app, a)
    plan = c.post(
        "/api/plans",
        json={
            "subject_id": s["id"],
            "equipment": [],
            "constraints": [],
            "key": "prepare",
        },
    ).json
    assert plan["status"] == "prepared", plan
    assert c.get("/api/me").json["trial"] is None
    original = plan["data"]["exercises"][0]["id"]
    replacement = plan["data"]["exercises"][1]["id"]
    assert (
        a.post(
            "/api/admin/plans/" + plan["id"] + "/replace",
            json={
                "revision": plan["revision"],
                "exercise_id": original,
                "replacement_id": replacement,
                "reason": "Try invalid replacement",
            },
        ).status_code
        == 422
    )
    lesson = c.post(
        "/api/lessons/start",
        json={
            "subject_id": s["id"],
            "plan_id": plan["id"],
            "equipment": [],
            "constraints": [],
            "key": "launch-plan",
        },
    ).json
    assert lesson["status"] == "started", lesson
    assert lesson["data"]["plan_id"] == plan["id"]
    assert c.get("/api/me").json["trial"] is not None


def test_feedback_changes_future_difficulty_only_by_published_rule(app):
    a, c = login(app), login(app, 2)
    s = subject(c, "child")
    exercises(app, a)
    first = c.post(
        "/api/lessons/start",
        json={"subject_id": s["id"], "equipment": [], "constraints": [], "key": "first"},
    ).json
    c.post(
        "/api/lessons/" + first["id"] + "/complete",
        json={
            "completed_exercises": [e["id"] for e in first["data"]["exercises"]],
            "feedback": "easy",
        },
    )
    for _ in range(2):
        publish(
            a,
            "exercise",
            {
                "source": "Synthetic fixture",
                "rights": "test",
                "instructions": "Approved harder fixture",
                "duration_seconds": 360,
                "min_months": 36,
                "max_months": 132,
                "difficulty": 2,
                "equipment": [],
                "exclusions": [],
                "video_id": "video",
                "video_verified": True,
            },
        )
    publish(
        a,
        "selection",
        {
            "source": "Synthetic fixture",
            "instructions": "Approved adaptation",
            "min_seconds": 600,
            "max_seconds": 900,
            "difficulty": 1,
            "allowed_difficulties": [1, 2],
            "adaptation": {"easy": 2, "hard": 1, "suitable": 1, "stop": 1},
            "examples": [{"expected_difficulty": 2}],
        },
    )
    app.clock[0] += 86400
    next_lesson = c.post(
        "/api/lessons/start",
        json={"subject_id": s["id"], "equipment": [], "constraints": [], "key": "next"},
    ).json
    assert all(ex["difficulty"] == 2 for ex in next_lesson["data"]["exercises"]), next_lesson
    assert all(ex["difficulty"] == 1 for ex in first["data"]["exercises"])


def test_archived_purchased_course_keeps_its_version_and_module_gates(app):
    a, c = login(app), login(app, 2)
    s = subject(c)
    owner = c.get("/api/me").json["account"]["id"]
    course = publish(
        a,
        "course",
        {
            "source": "Synthetic fixture",
            "rights": "test",
            "instructions": "Program",
            "audience": "adult",
            "min_months": 216,
            "max_months": 1440,
            "lessons": [
                {"id": "one", "title": "One", "text": "First content"},
                {"id": "two", "title": "Two", "text": "Second content"},
            ],
            "modules": [
                {"id": "m1", "title": "Start", "lesson_ids": ["one"], "requires": []},
                {"id": "m2", "title": "Next", "lesson_ids": ["two"], "requires": ["m1"]},
            ],
        },
    )
    with transaction(app.config["DATABASE"]) as db:
        insert(
            db,
            "right",
            {
                "service": "course",
                "course_id": course["id"],
                "starts": app.clock[0],
                "ends": app.clock[0] + 7200,
            },
            app.clock[0],
            owner,
            s["id"],
            "active",
        )
    a.post(
        "/api/admin/content/" + course["id"] + "/archive",
        json={"revision": course["revision"], "reason": "Close new sales"},
    )
    assert c.get("/api/my-courses").json["items"][0]["course"]["id"] == course["id"]
    path = "/api/courses/" + course["id"] + "/lessons/"
    assert c.get(path + "two?subject_id=" + s["id"]).status_code == 403
    c.post(path + "one/complete", json={"subject_id": s["id"]})
    assert c.get(path + "two?subject_id=" + s["id"]).json["text"] == "Second content"
