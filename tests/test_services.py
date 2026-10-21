from tests.test_identity import app, login
from tests.test_screening import publish, subject, method
from tests.test_commerce import policy, product, quote, pay


def buy(app, c, a, service, s, **extra):
    policy(a)
    p = product(a, service, **extra.pop("product_extra", {}))
    q = c.post(
        "/api/quotes",
        json={
            "product_id": p["id"],
            "subject_ids": [s["id"]],
            "surface": "telegram",
            **extra,
        },
    ).json
    assert "id" in q, q
    o = c.post("/api/orders", json={"quote_id": q["id"], "key": service}).json
    assert pay(app, o).status_code == 200
    return next(
        x
        for x in c.get("/api/history").json["items"]
        if x["kind"] == {"pdf": "report", "case": "case", "course": "right"}[service]
    )


def test_pdf_exact_revision_and_access(app):
    a, c, other = login(app), login(app, 2), login(app, 3)
    s = subject(c)
    m = publish(a, "method", method())
    attempt = c.post(
        "/api/attempts",
        json={"method_id": m["id"], "subject_id": s["id"], "key": "one"},
    ).json
    saved = c.patch(
        "/api/attempts/" + attempt["id"], json={"revision": 1, "answers": {"q1": "b"}}
    ).json
    c.post(
        "/api/attempts/" + attempt["id"] + "/complete",
        json={"revision": saved["revision"]},
    )
    report = buy(app, c, a, "pdf", s, attempt_id=attempt["id"])
    url = "/api/admin/reports/" + report["id"]
    assert report["data"]["due_at"] == app.clock[0] + 48 * 3600
    edited = a.post(
        url + "/revise",
        json={
            "revision": report["revision"],
            "text": "Персональный отчёт. Проверенный текст специалиста.",
            "reason": "Manual report",
        },
    ).json
    approved = a.post(
        url + "/approve",
        json={
            "revision": edited["revision"],
            "text_revision": 1,
            "reason": "Checked content",
        },
    ).json
    new = a.post(
        url + "/revise",
        json={
            "revision": approved["revision"],
            "text": "Новая редакция",
            "reason": "Changed content",
        },
    ).json
    assert (
        a.post(
            url + "/publish",
            json={
                "revision": new["revision"],
                "text_revision": 2,
                "reason": "Publish content",
            },
        ).status_code
        == 422
    )
    new = a.post(
        url + "/approve",
        json={
            "revision": new["revision"],
            "text_revision": 2,
            "reason": "Checked new content",
        },
    ).json
    res = a.post(
        url + "/publish",
        json={
            "revision": new["revision"],
            "text_revision": 2,
            "reason": "Publish content",
        },
    )
    assert res.status_code == 200, res.json
    pdf = c.get("/api/reports/" + report["id"] + "/pdf")
    assert pdf.data.startswith(b"%PDF")
    assert other.get("/api/reports/" + report["id"] + "/pdf").status_code == 404


def test_three_timers_and_single_followup(app):
    a, c = login(app), login(app, 2)
    s = subject(c)
    case = buy(app, c, a, "case", s, product_extra={"required_media": False})
    assert case["data"]["due_at"] is None
    app.clock[0] += 100
    sent = c.post(
        "/api/cases/" + case["id"] + "/submit",
        json={
            "problem": "Одна проблема",
            "answers": {"context": "Описание ситуации"},
            "key": "submit",
        },
    ).json
    assert sent["data"]["due_at"] == app.clock[0] + 48 * 3600
    answered = a.post(
        "/api/admin/cases/" + case["id"] + "/answer",
        json={
            "revision": sent["revision"],
            "text": "Ответ специалиста",
            "reason": "Checked answer",
        },
    ).json
    assert answered["data"]["followup_until"] == app.clock[0] + 24 * 3600
    app.clock[0] += 24 * 3600 - 1
    follow = c.post(
        "/api/cases/" + case["id"] + "/followup",
        json={"text": "Вопрос по той же проблеме", "same_problem": True},
    ).json
    assert follow["data"]["followup"]["due_at"] == app.clock[0] + 48 * 3600
    assert (
        c.post(
            "/api/cases/" + case["id"] + "/followup",
            json={"text": "Второй вопрос", "same_problem": True},
        ).status_code
        == 409
    )
    app.clock[0] += 2
    assert c.get("/api/cases/" + case["id"]).json["status"] == "followup_pending"


def test_booking_stale_proposal_and_repeat_confirmation(app):
    a, c = login(app), login(app, 2)
    s = subject(c)
    b = c.post(
        "/api/bookings",
        json={
            "subject_id": s["id"],
            "topic": "Запрос встречи",
            "preferences": "Утром",
            "key": "book",
        },
    ).json

    def proposal(obj, hour):
        return a.post(
            "/api/admin/bookings/" + b["id"] + "/propose",
            json={
                "revision": obj["revision"],
                "starts": app.clock[0] + hour * 3600,
                "duration_minutes": 60,
                "timezone": "Europe/Moscow",
                "reason": "Propose time",
            },
        ).json

    first = proposal(b, 10)
    second = proposal(first, 11)
    assert (
        c.post(
            "/api/bookings/" + b["id"] + "/confirm",
            json={"proposal_id": first["data"]["current_proposal"]},
        ).status_code
        == 409
    )
    for _ in range(2):
        assert (
            c.post(
                "/api/bookings/" + b["id"] + "/confirm",
                json={"proposal_id": second["data"]["current_proposal"]},
            ).status_code
            == 200
        )


def test_specialist_attachment_requires_publication_and_correction_keeps_window(app):
    import io

    a, c = login(app), login(app, 2)
    s = subject(c)
    case = buy(app, c, a, "case", s, product_extra={"required_media": False})
    sent = c.post(
        "/api/cases/" + case["id"] + "/submit",
        json={"problem": "One problem", "answers": {"context": "Context"}},
    ).json
    media = a.post(
        "/api/admin/cases/" + case["id"] + "/media",
        data={
            "reason": "Expert attachment",
            "file": (
                io.BytesIO(b"%PDF-1.4 synthetic test boundary"),
                "answer.pdf",
                "application/pdf",
            ),
        },
    ).json
    assert media["status"] == "ready", media
    assert c.get("/api/media/" + media["id"]).status_code == 404
    answered = a.post(
        "/api/admin/cases/" + case["id"] + "/answer",
        json={
            "revision": sent["revision"],
            "text": "Checked answer",
            "attachments": [media["id"]],
            "reason": "Publish checked response",
        },
    ).json
    assert c.get("/api/media/" + media["id"]).status_code == 200
    until = answered["data"]["followup_until"]
    app.clock[0] += 3600
    corrected = a.post(
        "/api/admin/cases/" + case["id"] + "/correct-answer",
        json={
            "revision": answered["revision"],
            "text": "Corrected text",
            "reason": "Correct typographical error",
        },
    ).json
    assert corrected["data"]["followup_until"] == until


def test_retention_never_deletes_media_of_an_active_case(app):
    import io
    from marina.operations import tick

    a, c = login(app), login(app, 2)
    s = subject(c)
    case = buy(app, c, a, "case", s, product_extra={"required_media": False})
    sent = c.post(
        "/api/cases/" + case["id"] + "/submit",
        json={"problem": "Problem", "answers": {"context": "Context"}},
    ).json
    media = a.post(
        "/api/admin/cases/" + case["id"] + "/media",
        data={
            "reason": "Expert attachment",
            "file": (io.BytesIO(b"%PDF-1.4 test fixture"), "answer.pdf", "application/pdf"),
        },
    ).json
    app.clock[0] += 86401
    assert (
        a.post(
            "/api/admin/media/" + media["id"] + "/delete",
            json={"revision": media["revision"], "reason": "Try while active"},
        ).status_code
        == 422
    )
    answered = a.post(
        "/api/admin/cases/" + case["id"] + "/answer",
        json={
            "revision": sent["revision"],
            "text": "Answer",
            "attachments": [media["id"]],
            "reason": "Checked response",
        },
    ).json
    app.clock[0] += 86401
    tick(app)
    assert (
        a.post(
            "/api/admin/media/" + media["id"] + "/delete",
            json={"revision": media["revision"], "reason": "Retention expired and service closed"},
        ).status_code
        == 200
    )
    assert c.get("/api/media/" + media["id"]).status_code == 404


def test_complete_case_before_payment_starts_sla_after_last_condition(app):
    from tests.test_commerce import policy, product, pay

    a, c = login(app), login(app, 2)
    s = subject(c)
    policy(a)
    p = product(a, "case", required_media=False)
    case = c.post(
        "/api/cases", json={"subject_id": s["id"], "product_id": p["id"], "key": "before-pay"}
    ).json
    assert case["data"]["paid_at"] is None, case
    sent = c.post(
        "/api/cases/" + case["id"] + "/submit",
        json={"problem": "One problem", "answers": {"context": "Full context"}},
    ).json
    assert sent["status"] == "awaiting_payment"
    assert sent["data"]["due_at"] is None
    app.clock[0] += 100
    q = c.post(
        "/api/quotes",
        json={
            "product_id": p["id"],
            "subject_ids": [s["id"]],
            "surface": "telegram",
            "case_id": case["id"],
        },
    ).json
    o = c.post("/api/orders", json={"quote_id": q["id"], "key": "fund-case"}).json
    assert pay(app, o).status_code == 200
    funded = c.get("/api/cases/" + case["id"]).json
    assert funded["status"] == "submitted"
    assert funded["data"]["due_at"] == app.clock[0] + 48 * 3600
    assert len([x for x in c.get("/api/history").json["items"] if x["kind"] == "case"]) == 1
