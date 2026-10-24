import copy

import pytest

from marina.bot import handle_update
from marina.db import connect
from marina.operations import tick, work_once
from tests.test_commerce import pay, policy, product
from tests.test_identity import app, login
from tests.test_screening import publish
from tests.funnel_fixtures import configure_funnel_source


@pytest.fixture(autouse=True)
def private_source(app, tmp_path):
    configure_funnel_source(app, tmp_path)


def scenario(admin, group="3-4", **overrides):
    response = admin.get("/api/admin/funnel-defaults/" + group)
    assert response.status_code == 200, response.json
    return publish(admin, "funnel", {**response.json, **overrides})


def start(client, group="3-4", key="one"):
    response = client.post("/api/funnel-runs", json={"age_group": group, "key": key})
    assert response.status_code == 200, response.json
    return response.json


def finish(client, run, values):
    for index, value in enumerate(values, 1):
        response = client.post(
            f"/api/funnel-runs/{run['id']}/answer",
            json={"revision": run["revision"], "question": f"q{index}", "value": value},
        )
        assert response.status_code == 200, response.json
        run = response.json
    return run


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
@pytest.mark.parametrize(
    "values,total,zone",
    [
        (["a"] * 6, 0, "green"),
        (["b"] * 3 + ["a"] * 3, 3, "green"),
        (["b"] * 4 + ["a"] * 2, 4, "yellow"),
        (["c"] + ["b"] * 5, 7, "yellow"),
        (["c"] * 4 + ["a"] * 2, 8, "red"),
        (["c"] * 6, 12, "red"),
    ],
)
def test_age_questionnaires_and_exact_score_boundaries(app, group, values, total, zone):
    admin, owner, stranger = login(app), login(app, 2), login(app, 3)
    scenario(admin, group)
    run = start(owner, group)
    assert (
        owner.post("/api/funnel-runs", json={"age_group": group, "key": "one"}).json["id"]
        == run["id"]
    )
    assert stranger.get("/api/funnel-runs/" + run["id"]).status_code == 404
    assert run["data"]["question"]["text"].startswith("[Вопрос 1 из 6]")
    assert "snapshot" not in run["data"]
    run = finish(owner, run, values)
    assert run["status"] == "completed"
    assert run["data"]["result"]["score"] == total
    assert run["data"]["result"]["zone"] == zone
    assert str(total) + " из 12" in run["data"]["result"]["text"]
    assert owner.get("/api/me").json["subjects"] == []
    assert "guide_text" not in str(owner.get("/api/data-export").json)


def approve_existing(admin, obj):
    for action in ("approve", "publish"):
        response = admin.post(
            f"/api/admin/content/{obj['id']}/{action}",
            json={"revision": obj["revision"], "reason": "Synthetic acceptance fixture"},
        )
        assert response.status_code == 200, response.json
        obj = response.json
    return obj


def drain(app):
    for _ in range(100):
        if not work_once(app):
            return
    raise AssertionError("Worker failed to drain the due outbox")


def capture(app):
    sent, files = [], []

    def transport(method, url, data, headers):
        sent.append((url.rsplit("/", 1)[-1], data))
        return {
            "ok": True,
            "result": "https://t.me/$synthetic-invoice"
            if url.endswith("/createInvoiceLink")
            else {},
        }

    app.config["HTTP_TRANSPORT"] = transport
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    return sent, files


def setup_sale(app, group="3-4"):
    app.config["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
    admin, owner = login(app), login(app, 2)
    policy(admin)
    base = scenario(admin, group)
    response = admin.post(
        f"/api/admin/funnels/{base['id']}/prepare-guide",
        json={"revision": base["revision"], "reason": "Prepare source PDF for review"},
    )
    assert response.status_code == 201, response.json
    material = approve_existing(admin, response.json)
    p = product(admin, "guide", amount=123, material_id=material["id"], age_group=group)
    config = scenario(
        admin, group, guide_product_id=p["id"], contact_url="https://t.me/synthetic_fixture"
    )
    sent, files = capture(app)
    run = finish(owner, start(owner, group), ["b"] * 6)
    app.clock[0] += 3
    drain(app)
    assert "ИТОГ СКРИНИНГА" in sent[-1][1]["text"]
    app.clock[0] += 2
    drain(app)
    assert "123 ⭐" in sent[-1][1]["text"]
    run = owner.get("/api/funnel-runs/" + run["id"]).json
    return admin, owner, run, config, material, sent, files


def test_consent_unpublished_copy_and_version_snapshots(app):
    admin, owner = login(app), login(app, 2)
    assert owner.get("/api/admin/funnel-defaults/3-4").status_code == 403
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "missing"}).status_code
        == 422
    )
    content = scenario(admin)
    app.config["ENFORCE_CONSENT"] = True
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "no-consent"}).status_code
        == 422
    )
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Separate privacy consent",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    assert (
        owner.post("/api/funnel-runs", json={"age_group": "3-4", "key": "not-accepted"}).status_code
        == 422
    )
    owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    run = start(owner)
    snapshot = copy.deepcopy(content["data"])
    snapshot["method"]["questions"][0]["text"] = "[Вопрос 1 из 6] Новая редакция"
    scenario(admin, **snapshot)
    assert (
        "Новая редакция"
        not in owner.get("/api/funnel-runs/" + run["id"]).json["data"]["question"]["text"]
    )
    newer = start(owner, key="newer")
    assert "Новая редакция" in newer["data"]["question"]["text"]
    assert (
        owner.post(
            f"/api/funnel-runs/{run['id']}/answer",
            json={"revision": run["revision"], "question": "q1", "value": "a"},
        ).status_code
        == 409
    )
    assert "guide_pages" not in str(owner.get("/api/data-export").json)


def test_question_sequence_stale_answers_and_resume(app):
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    run = start(owner)
    path = f"/api/funnel-runs/{run['id']}/answer"
    assert (
        owner.post(
            path, json={"revision": run["revision"], "question": "q6", "value": "c"}
        ).status_code
        == 422
    )
    command = {"revision": run["revision"], "question": "q1", "value": "b"}
    first = owner.post(path, json=command).json
    assert owner.post(path, json=command).json == first
    assert owner.post(path, json={**command, "value": "c"}).status_code == 409
    assert owner.get("/api/funnel-runs/" + run["id"]).json["data"]["question"]["id"] == "q2"
    assert (
        owner.post(
            path, json={"revision": first["revision"], "question": "q2", "value": "bad"}
        ).status_code
        == 422
    )
    sent, _ = capture(app)
    owner.post("/api/funnel-entries", json={"key": "new-start-before-resume"})
    resumed = owner.post(f"/api/funnel-runs/{run['id']}/resume", json={})
    assert resumed.json["data"]["question"]["id"] == "q2"
    app.clock[0] += 1200
    drain(app)
    assert not sent


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
def test_telegram_start_age_six_answers_and_result(app, group):
    from marina.funnel import AGES

    admin = login(app)
    scenario(admin, group)
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Accept before screening",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    app.config["ENFORCE_CONSENT"] = True
    sent, _ = capture(app)
    counter = iter(range(300, 600))

    def message(text):
        handle_update(
            app,
            {
                "update_id": next(counter),
                "message": {
                    "from": {"id": 2, "first_name": "Мама"},
                    "chat": {"id": 2, "type": "private"},
                    "text": text,
                },
            },
        )

    def click(title):
        button = next(
            b
            for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
            for b in row
            if b["text"] == title
        )
        handle_update(
            app,
            {
                "update_id": next(counter),
                "callback_query": {
                    "id": "fixture",
                    "from": {"id": 2, "first_name": "Мама"},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": 2, "type": "private"}},
                },
            },
        )

    message("/start")
    assert "Здравствуйте, Мама" in sent[-1][1]["text"]
    assert len(sent[-1][1]["reply_markup"]["inline_keyboard"]) == 5
    click(AGES[group])
    assert sent[-1][1]["text"] == privacy["data"]["terms"]
    click("Прочитал(а) и принимаю")
    for number in range(1, 7):
        assert f"[Вопрос {number} из 6]" in sent[-1][1]["text"]
        assert [r[0]["text"] for r in sent[-1][1]["reply_markup"]["inline_keyboard"]] == [
            "А",
            "Б",
            "В",
        ]
        click("Б")
    assert "Обрабатываю" in sent[-1][1]["text"]
    app.clock[0] += 3
    tick(app)
    drain(app)
    assert "6 из 12" in sent[-1][1]["text"]
    assert "диагноз" in sent[-1][1]["text"]


def test_age_reminder_survives_restart_and_is_cancelled_on_selection(app):
    owner = login(app, 2)
    sent, _ = capture(app)
    entry = owner.post("/api/funnel-entries", json={"key": "first"}).json
    assert owner.post("/api/funnel-entries", json={"key": "first"}).json["id"] == entry["id"]
    app.clock[0] += 1199
    drain(app)
    assert not sent
    app.clock[0] += 1
    drain(app)
    assert "отвлеклись" in sent[-1][1]["text"]
    count = len(sent)
    tick(app)
    drain(app)
    assert len(sent) == count
    second = owner.post("/api/funnel-entries", json={"key": "second"}).json
    owner.post(f"/api/funnel-entries/{second['id']}/age", json={"age_group": "outside"})
    app.clock[0] += 1200
    drain(app)
    assert len(sent) == count


def test_guide_payment_delivery_idempotency_and_nurture(app):
    admin, owner, run, config, material, sent, files = setup_sale(app)
    assert owner.get("/api/admin/materials/" + material["id"] + "/pdf").status_code == 403
    assert admin.get("/api/admin/materials/" + material["id"] + "/pdf").data.startswith(b"%PDF")
    response = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 200, response.json
    order = response.json
    assert owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json["id"] == order["id"]
    assert order["data"]["total"] == 123
    assert order["data"]["currency"] == "XTR"
    attempt = owner.post(f"/api/orders/{order['id']}/checkout", json={}).json
    assert attempt["status"] == "queued"
    drain(app)
    assert (
        sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"]
        == "https://t.me/$synthetic-invoice"
    )
    assert not files
    assert pay(app, order).status_code == 200
    assert pay(app, order).status_code == 200
    drain(app)
    assert len(files) == 1 and files[0][2].startswith(b"%PDF")
    completed = owner.get("/api/funnel-runs/" + run["id"]).json
    assert config["data"]["buyer_tag"] in completed["data"]["tags"]
    right_id = completed["data"]["guide_right_id"]
    assert owner.get(f"/api/guides/{right_id}/pdf").data == files[0][2]
    assert login(app, 3).get(f"/api/guides/{right_id}/pdf").status_code == 404
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM objects WHERE kind='right'").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE object_id=? AND event LIKE 'funnel.followup.%'",
                (run["id"],),
            ).fetchone()[0]
            == 3
        )
        assert (
            db.execute(
                "SELECT count(*) FROM outbox WHERE object_id=? AND event LIKE 'funnel.cart.%' AND status='pending'",
                (run["id"],),
            ).fetchone()[0]
            == 0
        )
    for _ in range(3):
        app.clock[0] += 86400
        drain(app)
    assert "TEST_PROMO" in sent[-1][1]["text"]
    assert "сегодня в 23:59" not in sent[-1][1]["text"]


def test_refusal_stop_and_expiry_cancel_sales_messages(app):
    _, owner, run, config, _, sent, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    app.clock[0] += 1800
    drain(app)
    assert "не завершили оплату" in sent[-1][1]["text"]
    renewed = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert renewed["id"] != order["id"]
    assert renewed["data"]["expires"] <= run["data"]["offer_expires"]
    app.config["ENFORCE_CONSENT"] = True
    response = owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={})
    assert response.status_code == 200, response.json
    assert config["data"]["refused_tag"] in response.json["data"]["tags"]
    assert owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).status_code == 422
    count = len(sent)
    app.clock[0] += 7200
    drain(app)
    assert len(sent) == count


def test_booking_suppresses_followups_and_keeps_existing_scheduling_flow(app):
    admin, owner, run, _, _, sent, _ = setup_sale(app)
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Вторник, после 18:00 МСК"}
    ).json
    assert booking["status"] == "requested"
    assert (
        owner.post(f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Повтор"}).json["id"]
        == booking["id"]
    )
    proposed = admin.post(
        f"/api/admin/bookings/{booking['id']}/propose",
        json={
            "revision": booking["revision"],
            "reason": "Agree a time",
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
        },
    ).json
    drain(app)
    assert any("Подтвердите время" in data.get("text", "") for _, data in sent)
    assert (
        owner.post(
            f"/api/bookings/{booking['id']}/confirm",
            json={"proposal_id": proposed["data"]["current_proposal"]},
        ).json["status"]
        == "confirmed"
    )
    stopped = owner.post("/api/funnel/stop", json={})
    assert stopped.status_code == 200


def test_profile_changes_are_audited_and_partial_delivery_can_resume(app):
    admin, owner = login(app), login(app, 2)
    calls = []

    def transport(method, url, data, headers):
        method = url.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "setMyDescription" and calls.count(method) == 1:
            raise RuntimeError("Synthetic interruption")
        return {"ok": True, "result": True}

    app.config["HTTP_TRANSPORT"] = transport
    data = admin.get("/api/admin/funnel-profile").json
    assert (
        owner.post("/api/admin/funnel-profile", json={**data, "reason": "Fixture"}).status_code
        == 403
    )
    assert admin.post("/api/admin/funnel-profile", json=data).status_code == 422
    response = admin.post(
        "/api/admin/funnel-profile", json={**data, "reason": "Set owner-supplied profile"}
    )
    assert response.status_code == 202
    obj = response.json
    assert not calls
    drain(app)
    assert admin.get("/api/admin/objects/" + obj["id"]).json["object"]["status"] == "partial"
    app.clock[0] += 60
    drain(app)
    assert calls.count("setMyName") == 1 and calls.count("setMyDescription") == 2
    assert admin.get("/api/admin/objects/" + obj["id"]).json["object"]["status"] == "applied"
    assert admin.get(f"/api/admin/objects/{obj['id']}/versions").json["items"]


@pytest.mark.parametrize("stop_kind", ["client", "admin", "booking", "revoke"])
def test_paid_guide_retry_and_followup_cancellation(app, stop_kind):
    admin, owner, run, _, _, sent, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    transport = app.config["HTTP_TRANSPORT"]
    failed = []

    def failing_success(method, url, data, headers):
        if "Оплата прошла успешно" in data.get("text", "") and not failed:
            failed.append(True)
            raise RuntimeError("Synthetic sendMessage failure after sendDocument")
        return transport(method, url, data, headers)

    app.config["HTTP_TRANSPORT"] = failing_success
    drain(app)
    assert len(files) == 1
    app.clock[0] += 60
    drain(app)
    assert len(files) == 1
    current = owner.get("/api/funnel-runs/" + run["id"]).json
    assert current["data"]["guide_delivered_at"]
    if stop_kind == "client":
        owner.post("/api/funnel/stop", json={})
    elif stop_kind == "admin":
        assert (
            owner.post(
                f"/api/admin/funnel-runs/{run['id']}/stop",
                json={"revision": current["revision"], "reason": "Fixture"},
            ).status_code
            == 403
        )
        assert (
            admin.post(
                f"/api/admin/funnel-runs/{run['id']}/stop",
                json={"revision": current["revision"], "reason": "Stop marketing"},
            ).status_code
            == 200
        )
    elif stop_kind == "booking":
        booking = owner.post(
            f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Вторник, 18:00 МСК"}
        ).json
        assert booking["data"]["promotion"]["eligible"] is True
        drain(app)
    else:
        right = admin.get("/api/admin/objects/" + current["data"]["guide_right_id"]).json["object"]
        revoked = admin.post(
            f"/api/admin/rights/{right['id']}/revoke",
            json={"revision": right["revision"], "reason": "Revoke test right"},
        )
        assert revoked.status_code == 200, revoked.json
        assert owner.get(f"/api/guides/{right['id']}/pdf").status_code == 403
    count = len(sent)
    app.clock[0] += 3 * 86400
    drain(app)
    assert len(sent) == count


@pytest.mark.parametrize("late", [False, True])
def test_unverified_or_late_payment_cannot_deliver_guide(app, late):
    admin, owner, run, _, _, _, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    if late:
        app.clock[0] += 901
    assert pay(app, order, amount=123 if late else 1).status_code == 200
    drain(app)
    assert not files
    assert not admin.get("/api/admin/objects?kind=right").json["items"]
    assert admin.get("/api/admin/objects?kind=reconciliation").json["items"]


def test_green_free_pdf_and_red_consultation_have_no_paid_gate(app):
    admin, owner = login(app), login(app, 2)
    base = scenario(admin)
    material = admin.post(
        f"/api/admin/funnels/{base['id']}/prepare-guide",
        json={"revision": base["revision"], "reason": "Synthetic free PDF"},
    ).json
    material = approve_existing(admin, material)
    scenario(admin, free_material_id=material["id"])
    sent, files = capture(app)
    green = finish(owner, start(owner), ["a"] * 6)
    app.clock[0] += 3
    drain(app)
    assert "Игры по возрасту" in str(sent[-1])
    assert owner.get(f"/api/funnel-runs/{green['id']}/free-pdf").data.startswith(b"%PDF")
    red = finish(owner, start(owner, key="red"), ["c"] * 6)
    app.clock[0] += 3
    drain(app)
    assert "О диагностике" in str(sent[-1])
    assert owner.get(f"/api/funnel-runs/{red['id']}/free-pdf").status_code == 403
    assert (
        "промокод"
        not in owner.get(f"/api/funnel-runs/{red['id']}/consultation").json["text"].lower()
    )
    assert (
        owner.post(
            f"/api/funnel-runs/{red['id']}/book", json={"preferences": "Завтра утром МСК"}
        ).json["status"]
        == "requested"
    )


def test_refusal_can_deliver_the_configured_free_video(app, tmp_path):
    import io
    from tests.test_crm import make_video

    admin, owner = login(app), login(app, 2)
    raw = make_video(tmp_path)
    material = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Synthetic free video",
            "data": {"owner": "Fixture specialist", "status": "received"},
            "reason": "Fixture free video",
        },
    ).json
    asset = admin.post(
        f"/api/admin/content/{material['id']}/asset",
        data={"reason": "Fixture MP4", "file": (io.BytesIO(raw), "fixture.mp4", "video/mp4")},
    ).json
    material = admin.patch(
        f"/api/admin/content/{material['id']}",
        json={
            "revision": material["revision"],
            "data": {**material["data"], "asset_id": asset["id"]},
            "reason": "Attach reviewed fixture video",
        },
    ).json
    material = approve_existing(admin, material)
    scenario(admin, refusal_material_id=material["id"])
    _, files = capture(app)
    run = finish(owner, start(owner), ["b"] * 6)
    assert owner.post(f"/api/funnel-runs/{run['id']}/refuse", json={}).status_code == 200
    app.clock[0] += 4 * 86400
    drain(app)
    assert len(files) == 1
    assert files[0][2] == raw and files[0][4] == "sendVideo"
