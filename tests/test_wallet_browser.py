"""Standalone wallet browser smoke: login, read-only balance and strict CSP."""

import re
import threading

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from marina.bot import Bot
from tests.test_identity import app as identity_app


def test_external_wallet_login_and_default_disabled_checkout(tmp_path):
    app = identity_app.__wrapped__(tmp_path)
    app.config.update(NEUROCOINS_LAVA_ENABLED=False, NEUROCOINS_WEB_ORIGIN="")
    messages = []

    def transport(method, url, data, headers):
        if url.endswith("/sendMessage"):
            messages.append(data)
        return {"ok": True, "result": {"message_id": len(messages)}}

    app.config["HTTP_TRANSPORT"] = transport
    bot = Bot(app, {"id": 2, "first_name": "Parent"}, 2, "browser-login")
    bot.message({"text": "/login"})
    code = re.findall(r"\b[A-Za-z0-9_-]{40,60}\b", messages[-1]["text"])[0]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                js_errors = []
                page.on("pageerror", lambda error: js_errors.append(str(error)))
                response = page.goto(f"http://127.0.0.1:{server.server_port}/wallet")
                assert response.status == 200
                assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
                page.locator('input[type="text"]').fill(code)
                page.get_by_role("button", name="Войти").click()
                page.get_by_text("Мой баланс: 0 НК").wait_for()
                assert page.get_by_text("Пополнение пока недоступно").is_visible()
                assert not page.get_by_role("button", name="Получить счёт Lava.top").count()
                assert not js_errors, js_errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
