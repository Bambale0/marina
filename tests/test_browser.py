import hashlib
import threading

import pytest
from werkzeug.serving import make_server

from marina.db import transaction
from tests.test_identity import app, login
from tests.test_screening import publish


def test_browser_admin_and_profile_forms(app):
    sync_api = pytest.importorskip("playwright.sync_api")
    a = login(app)
    p = publish(
        a,
        "policy",
        {
            "type": "privacy",
            "terms": "Test privacy notice",
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )
    a.post("/api/consents", json={"policy_id": p["id"], "accepted": True})
    owner = a.get("/api/me").json["account"]["id"]
    code = "test-browser-code-very-long-unique"
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO login_codes VALUES(?,?,?)",
            (hashlib.sha256(code.encode()).hexdigest(), owner, app.clock[0] + 300),
        )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1360, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("https://telegram.org/**", lambda route: route.abort())
            page.goto("http://127.0.0.1:" + str(server.server_port))
            page.get_by_label("Одноразовый код из команды /login в боте").fill(code)
            page.get_by_role("button", name="Войти", exact=True).click()
            page.get_by_role("button", name="Профили", exact=True).click()
            page.get_by_label("Имя", exact=True).fill("Adult fixture")
            page.get_by_label("Дата рождения", exact=True).fill("1990-01-01")
            page.get_by_role("button", name="Создать", exact=True).click()
            page.get_by_role("heading", name="Adult fixture").wait_for()
            page.get_by_role("button", name="Контент и настройки", exact=True).click()
            page.get_by_role("button", name="Создать черновик", exact=True).click()
            page.get_by_label("Название", exact=True).fill("UI decision fixture")
            page.get_by_label("Ответственный", exact=True).fill("Owner")
            page.get_by_label("Основание / причина действия", exact=True).fill("Browser acceptance")
            page.get_by_role("button", name="Сохранить черновик", exact=True).click()
            page.get_by_text("Черновик сохранён", exact=True).wait_for()
            page.get_by_role("button", name="Контент и настройки", exact=True).click()
            page.get_by_label("Раздел", exact=True).select_option("onboarding")
            page.get_by_role("button", name="Создать черновик", exact=True).click()
            page.get_by_label("Возрастная группа", exact=True).select_option("7-8")
            page.get_by_role("button", name="Продолжить", exact=True).click()
            page.get_by_label("Название", exact=True).wait_for()
            assert page.get_by_label("Название", exact=True).input_value() == "Знакомство: 7–8 лет"
            texts = page.get_by_label("Текст", exact=True)
            assert texts.count() == 7
            texts.first.fill("Отредактированный вопрос?")
            page.get_by_label("Основание / причина действия", exact=True).fill("Edit introduction")
            page.get_by_role("button", name="Сохранить черновик", exact=True).click()
            page.get_by_text("Черновик сохранён", exact=True).wait_for()
            assert (
                page.get_by_label("Текст", exact=True).first.input_value()
                == "Отредактированный вопрос?"
            )
            page.get_by_role("button", name="Все объекты", exact=True).click()
            page.get_by_role("heading", name="Все объекты", exact=True).wait_for()
            page.get_by_role("button", name="Отчёты", exact=True).click()
            page.get_by_role("heading", name="Операционные отчёты", exact=True).wait_for()
            assert not errors, errors
            page.get_by_role("button", name="Обзор", exact=True).click()
            page.screenshot(path="var/desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="var/mobile.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            app.clock[0] += 30 * 24 * 3600
            page.reload()
            page.get_by_label("Одноразовый код из команды /login в боте").wait_for()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
