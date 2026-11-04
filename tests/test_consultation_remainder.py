import copy
import io
import json
from pathlib import Path
import pytest
from marina import Problem
from marina.bot import Bot
from marina.operations import notify_consultation
from tests.test_consultation_commerce import (
    app,
    confirmed_consultation,
    CASES,
    quote,
    pay_consultation,
)
from tests.test_funnel import scenario, start, finish, drain
from tests.test_screening import publish
from tests.test_commerce import pay
from tests.test_crm import make_video

SOURCE = Path(__file__).resolve().parents[1] / "docs/content/consultation-remainder-20261105.json"


def journey_sale(app, group="3-4", base=600000, promo=3900, coupon=False, prepay=True, book=True):
    admin, owner, _, _, tariff, _, sent, files = confirmed_consultation(
        app, group, base, promo, paid_pdf=False, confirm=False
    )
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava", "stars"]
    from marina.db import connect
    from marina.funnel import published

    with connect(app.config["DATABASE"]) as db:
        current = published(db, group)
    guide_id = current["data"]["guide_product_id"]
    guide = admin.get("/api/admin/content/" + guide_id).json
    j = copy.deepcopy(json.loads(SOURCE.read_text())["ages"][group])
    j.update(
        payment_timing="before_scheduling" if prepay else "after_scheduling",
        preparation_material_id=guide["data"]["material_id"],
    )
    scenario(
        admin,
        group,
        guide_product_id=guide_id,
        consultation_product_id=tariff["id"],
        consultation_journey=j,
        contact_url="https://t.me/synthetic_fixture",
    )
    p = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Synthetic parent consent",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    owner.post("/api/consents", json={"policy_id": p["id"], "accepted": True})
    run = finish(owner, start(owner, group, key="new-journey"), ["b"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    app.clock[0] += 180
    drain(app)
    if coupon:
        guide_order = owner.post("/api/funnel-runs/" + run["id"] + "/purchase", json={})
        assert guide_order.status_code == 200, guide_order.json
        assert pay(app, guide_order.json).status_code == 200
        drain(app)
    app.config["ENABLED_PAYMENT_PROVIDERS"] = ["lava"]
    booking = None
    if book:
        response = owner.post(
            "/api/funnel-runs/" + run["id"] + "/book",
            json={
                "answers": {
                    "profile": "Имя ребёнка, возраст и класс",
                    "problem": "custom:Свой запрос родителя",
                    "slot": "option_2",
                }
            },
        )
        assert response.status_code == 200, response.json
        booking = response.json
    return admin, owner, run, booking, sent, files, j


@pytest.mark.parametrize("group,base,promo", CASES)
@pytest.mark.parametrize("coupon", [False, True])
def test_exact_price_and_preparation_before_scheduling(app, group, base, promo, coupon):
    _, owner, _, booking, sent, files, j = journey_sale(app, group, base, promo, coupon)
    assert booking["status"] == "requested"
    assert booking["data"]["intake_display"]["profile"] == "Имя ребёнка, возраст и класс"
    assert (
        booking["data"]["intake_variables"][j["questions"][0]["storage_key"]]
        == "Имя ребёнка, возраст и класс"
    )
    assert len(booking["data"]["intake_variables"]) == 3
    q = quote(owner, booking, amount=1)
    assert q.status_code == 201, q.json
    assert q.json["data"]["total"] == (promo * 100 if coupon else base)
    order = owner.post("/api/orders", json={"quote_id": q.json["id"], "key": "prepay"}).json
    before = len(files)
    assert pay_consultation(app, owner, order).status_code == 200
    drain(app)
    assert len(files) == before + 1
    assert files[-1][1] == j["preparation_filename"]
    assert any("Имя ребёнка" in d.get("text", "") for _, d in sent)
    assert pay_consultation(app, owner, order).status_code == 200
    drain(app)
    assert len(files) == before + 1
    paid = owner.get("/api/bookings/" + booking["id"]).json
    assert paid["status"] == "requested" and paid["data"]["paid_at"] is not None


def test_requires_author_pdf_to_approve_early_payment(app):
    from tests.test_identity import login

    admin = login(app)
    j = copy.deepcopy(json.loads(SOURCE.read_text())["ages"]["3-4"])
    j["payment_timing"] = "before_scheduling"
    defaults = admin.get("/api/admin/funnel-defaults/3-4").json
    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "funnel",
            "title": "Missing author PDF",
            "data": {**defaults, "consultation_journey": j},
            "reason": "Synthetic readiness",
        },
    ).json
    r = admin.post(
        "/api/admin/content/" + draft["id"] + "/approve",
        json={"revision": draft["revision"], "reason": "Synthetic check"},
    )
    assert r.status_code == 422
    assert "памят" in r.json["error"].lower()


def test_old_scheduling_flow_is_preserved_until_author_material_ready(app):
    _, owner, _, booking, _, _, _ = journey_sale(app, prepay=False)
    assert booking["data"]["payment_timing"] == "after_scheduling"
    assert quote(owner, booking).status_code == 422


def test_bot_three_answers_custom_option_and_stale_callback(app):
    _, owner, run, _, _, _, _ = journey_sale(app, prepay=False, book=False)
    bot = Bot(app, {"id": 2, "first_name": "Мама"}, 2, "intake")
    bot.action("intake_start", {"id": run["id"]})
    draft = bot.state["data"]["consultation_intake"]
    from marina.consultation_intake_bot import handle_text

    handle_text(bot, "Имя, 3 года 2 месяца")
    old = {"draft_id": draft["id"], "question": "problem", "value": "other"}
    bot.action("intake_choice", old)
    handle_text(bot, "Свой запрос")
    with pytest.raises(Problem):
        bot.action("intake_choice", old)
    bot.action("intake_choice", {"draft_id": draft["id"], "question": "slot", "value": "option_1"})
    assert bot.state["data"]["step"] is None
    booking = owner.get("/api/funnel-runs/" + run["id"]).json["data"]["booking_id"]
    assert (
        owner.get("/api/bookings/" + booking).json["data"]["intake_answers"]["problem"]
        == "custom:Свой запрос"
    )


def test_source_rejects_tampered_answers_and_uses_full_price_without_coupon(app):
    _, owner, run, _, _, _, _ = journey_sale(app, book=False)
    for answers in (
        {"profile": "x"},
        {"profile": "x", "problem": "injected", "slot": "option_1"},
        {"profile": "x", "problem": "option_1", "slot": "fake"},
    ):
        r = owner.post(
            "/api/funnel-runs/" + run["id"] + "/book",
            json={"answers": answers, "payment_timing": "before_scheduling", "amount": 1},
        )
        assert r.status_code == 422, r.json
    details = owner.get("/api/funnel-runs/" + run["id"] + "/consultation-journey").json
    assert details["amount_rub"] == 6000 and details["eligible"] is False


def route_setup(app):
    from tests.test_identity import login

    admin, owner = login(app), login(app, 2)
    config = json.loads(SOURCE.read_text())["outside"]
    p = publish(admin, "policy", config)
    privacy = publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Synthetic parent consent",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    owner.post("/api/consents", json={"policy_id": privacy["id"], "accepted": True})
    app.config["HTTP_TRANSPORT"] = lambda *args: {"ok": True, "result": {"message_id": 1}}
    app.config["FILE_TRANSPORT"] = lambda *args: None
    return admin, owner, p


@pytest.mark.parametrize(
    "category,price", [("baby_0_2", 1900), ("teen_senior", 4500), ("adult", 5000)]
)
def test_qualified_leads_are_idempotent_and_notify_only_admin(app, category, price):
    admin, owner, p = route_setup(app)
    data = {"route": category, "action": "contact", "key": "same"}
    first = owner.post("/api/consultation-leads", json=data)
    assert first.status_code == 201, first.json
    assert owner.post("/api/consultation-leads", json=data).json["id"] == first.json["id"]
    assert first.json["data"]["amount_rub"] == price
    assert first.json["data"]["policy_id"] == p["id"]
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": len(calls)},
    }
    drain(app)
    notices = [d for d in calls if "НЕСТАНДАРТНЫЙ" in d.get("text", "")]
    assert len(notices) == 1 and notices[0]["chat_id"] == 1
    assert admin.get("/api/admin/queue").status_code == 200


def test_no_nonexistent_free_file_is_claimed_or_delivered(app):
    _, owner, _ = route_setup(app)
    r = owner.post(
        "/api/consultation-leads",
        json={"route": "baby_0_2", "action": "download", "key": "download"},
    )
    assert r.status_code == 404


def task_setup(app):
    admin, owner, _ = route_setup(app)
    lead = owner.post(
        "/api/consultation-leads", json={"route": "baby_0_2", "action": "booking", "key": "task"}
    ).json
    r = owner.post("/api/video-tasks", json={"lead_id": lead["id"]})
    assert r.status_code == 201, r.json
    assert owner.post("/api/video-tasks", json={"lead_id": lead["id"]}).json["id"] == r.json["id"]
    return admin, owner, r.json


def test_video_assignment_upload_review_and_recommendations(app, tmp_path):
    from tests.test_identity import login

    admin, owner, task = task_setup(app)
    stranger = login(app, 3)
    assert stranger.get("/api/video-tasks/" + task["id"]).status_code == 404
    assert owner.post("/api/video-tasks/" + task["id"] + "/submit", json={}).status_code == 422
    raw = make_video(tmp_path)
    before_assignment = owner.post(
        "/api/video-tasks/" + task["id"] + "/video",
        data={"slot": "1", "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
    )
    assert before_assignment.status_code == 409
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Авторское задание специалиста: три движения в игре",
            "reason": "Назначение специалиста",
        },
    )
    assert assigned.status_code == 200, assigned.json
    for slot in range(1, 4):
        r = owner.post(
            "/api/video-tasks/" + task["id"] + "/video",
            data={"slot": str(slot), "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
        )
        assert r.status_code == 200, r.json
        assert (
            stranger.get("/api/video-tasks/" + task["id"] + "/video/" + str(slot)).status_code
            == 404
        )
    repeat = owner.post(
        "/api/video-tasks/" + task["id"] + "/video",
        data={"slot": "3", "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
    )
    assert len(repeat.json["data"]["videos"]) == 3
    submitted = owner.post("/api/video-tasks/" + task["id"] + "/submit", json={})
    assert submitted.status_code == 200, submitted.json
    assert submitted.json["data"]["due_at"] == app.clock[0] + 24 * 3600
    assert (
        owner.post("/api/admin/video-tasks/" + task["id"] + "/recommendations", json={}).status_code
        == 403
    )
    answer = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/recommendations",
        json={
            "revision": submitted.json["revision"],
            "text": "Разбор выполнения, коррекция движений и индивидуальные рекомендации",
            "reason": "Ручной разбор специалистом",
        },
    )
    assert answer.status_code == 200, answer.json
    assert (
        owner.get("/api/video-tasks/" + task["id"])
        .json["data"]["recommendations"]
        .startswith("Разбор выполнения")
    )
    assert answer.json["status"] == "completed"
    calls = []
    files = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": len(calls)},
    }
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    drain(app)
    assert len(files) == 3
    assert any("Авторское задание" in d.get("text", "") and d["chat_id"] == 2 for d in calls)
    assert any(
        "индивидуальные рекомендации" in d.get("text", "") and d["chat_id"] == 2 for d in calls
    )


def test_parent_video_task_in_real_browser(app, tmp_path):
    import threading
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server

    admin, owner, task = task_setup(app)
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Авторское задание: выполните три игровых действия",
            "reason": "Выдано специалистом",
        },
    )
    assert assigned.status_code == 200
    path = tmp_path / "upload.mp4"
    path.write_bytes(make_video(tmp_path))
    token = owner.environ_base["HTTP_AUTHORIZATION"].removeprefix("Bearer ")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script("sessionStorage.setItem('marina-token'," + json.dumps(token) + ")")
            page.route("https://telegram.org/**", lambda route: route.abort())
            page.goto("http://127.0.0.1:" + str(server.server_port))

            def open_task():
                page.get_by_role("button", name="Мои обращения", exact=True).click()
                page.locator("article").filter(
                    has=page.get_by_role(
                        "heading", name="Видеоразбор выполнения задания", exact=True
                    )
                ).get_by_role("button", name="Открыть", exact=True).click()

            open_task()
            expect(
                page.get_by_text("Авторское задание: выполните три игровых действия", exact=True)
            ).to_be_visible()
            for slot in range(1, 4):
                page.get_by_label(
                    "Видео выполнения пункта " + str(slot) + " (MP4, до 60 секунд)", exact=True
                ).set_input_files(str(path))
                page.get_by_role("heading", name="Пункт " + str(slot), exact=True).locator(
                    ".."
                ).get_by_role("button", name="Сохранить видео", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Мои обращения", exact=True)
                ).to_be_visible()
                open_task()
            page.get_by_role("button", name="Отправить специалисту на разбор", exact=True).click()
            expect(page.get_by_role("heading", name="Мои обращения", exact=True)).to_be_visible()
            current = owner.get("/api/video-tasks/" + task["id"]).json
            assert current["status"] == "submitted"
            response = admin.post(
                "/api/admin/video-tasks/" + task["id"] + "/recommendations",
                json={
                    "revision": current["revision"],
                    "text": "Ручная коррекция и рекомендации специалиста",
                    "reason": "Проверенный разбор",
                },
            )
            assert response.status_code == 200
            open_task()
            expect(
                page.get_by_text("Ручная коррекция и рекомендации специалиста", exact=True)
            ).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()


def test_audio_response_is_private_until_expert_publication(app, tmp_path):
    import subprocess

    admin, owner, task = task_setup(app)
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Три игровых задания специалиста",
            "reason": "Авторское назначение",
        },
    ).json
    raw = make_video(tmp_path)
    for slot in range(1, 4):
        assert (
            owner.post(
                "/api/video-tasks/" + task["id"] + "/video",
                data={"slot": str(slot), "file": (io.BytesIO(raw), "v.mp4", "video/mp4")},
            ).status_code
            == 200
        )
    submitted = owner.post("/api/video-tasks/" + task["id"] + "/submit", json={}).json
    audio = tmp_path / "recommendations.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-t",
            "0.2",
            "-c:a",
            "libopus",
            str(audio),
        ],
        check=True,
        timeout=20,
    )
    upload = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/media",
        data={
            "reason": "Личный аудиоразбор специалиста",
            "file": (io.BytesIO(audio.read_bytes()), "recommendations.ogg", "audio/ogg"),
        },
    )
    assert upload.status_code == 201, upload.json
    aid = upload.json["id"]
    assert owner.get("/api/media/" + aid).status_code == 404
    result = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/recommendations",
        json={
            "revision": submitted["revision"],
            "text": "Индивидуальная коррекция и рекомендации — в аудио",
            "attachments": [aid],
            "reason": "Опубликован проверенный аудиоразбор",
        },
    )
    assert result.status_code == 200, result.json
    assert owner.get("/api/media/" + aid).status_code == 200
    assert "assigned_by" not in owner.get("/api/video-tasks/" + task["id"]).json["data"]
    assert any(r["id"] == task["id"] for r in owner.get("/api/history").json["items"])
    sent = []
    app.config["FILE_TRANSPORT"] = lambda *args: sent.append(args)
    drain(app)
    assert len([args for args in sent if args[1] == "recommendations.ogg"]) == 1
