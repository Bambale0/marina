from marina.ai import context
from marina.db import transaction, insert
from marina.operations import work_once
from tests.test_identity import app, login
from tests.test_screening import publish, method, subject
from tests.test_services import buy


def test_ai_only_allowlisted_text_stays_unpublished(app):
    a, c = login(app), login(app, 2)
    s = subject(c)
    m = publish(a, "method", method())
    attempt = c.post(
        "/api/attempts", json={"method_id": m["id"], "subject_id": s["id"], "key": "ai"}
    ).json
    saved = c.patch(
        "/api/attempts/" + attempt["id"], json={"revision": 1, "answers": {"q1": "b"}}
    ).json
    c.post(
        "/api/attempts/" + attempt["id"] + "/complete",
        json={"revision": saved["revision"]},
    )
    report = buy(app, c, a, "pdf", s, attempt_id=attempt["id"])
    publish(
        a,
        "policy",
        {
            "type": "privacy",
            "terms": "Test processing terms",
            "media_max_bytes": 10000,
            "retention_days": 30,
            "allow_external_ai": True,
        },
    )
    publish(
        a,
        "policy",
        {
            "type": "ai",
            "terms": "Test model settings",
            "model": "test-model",
            "max_output_tokens": 500,
            "daily_jobs": 2,
        },
    )
    template = publish(
        a,
        "template",
        {"type": "report", "text": "Use only approved data", "source_ids": []},
    )
    captured = []

    def transport(method, url, data, headers):
        captured.append(data)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "SECRET AI DRAFT"}],
                }
            ],
        }

    app.config.update(OPENAI_API_KEY="test-key", HTTP_TRANSPORT=transport)
    res = a.post(
        "/api/admin/reports/" + report["id"] + "/ai-draft",
        json={
            "revision": report["revision"],
            "template_id": template["id"],
            "reason": "Create text draft",
        },
    )
    assert res.status_code == 202, res.json
    for _ in range(10):
        if not work_once(app, send=lambda *args: None):
            break
    assert captured[0]["store"] is False
    assert captured[0]["tools"] == []
    for forbidden in ("birth_date", "media", "video", "https://", "Fixture"):
        assert forbidden not in captured[0]["input"]
    assert "SECRET AI DRAFT" not in str(c.get("/api/history").json)
    assert c.get("/api/reports/" + report["id"] + "/pdf").status_code == 404
    assert (
        a.get("/api/admin/objects/" + report["id"]).json["object"]["data"]["approved_revision"]
        is None
    )
