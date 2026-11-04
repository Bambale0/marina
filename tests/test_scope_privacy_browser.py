import json
import threading

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from tests.test_identity import app, login
from tests.test_privacy_documents import privacy
from tests.test_product_scope import restrict


def test_removed_services_and_separate_unchecked_consents_in_browser(app):
    admin, owner = login(app), login(app, 2)
    restrict(admin)
    privacy(admin)
    token = owner.environ_base["HTTP_AUTHORIZATION"].removeprefix("Bearer ")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.add_init_script(
                "sessionStorage.setItem('marina-token', " + json.dumps(token) + ");"
            )
            page.route("https://telegram.org/**", lambda route: route.abort())
            page.goto("http://127.0.0.1:" + str(server.server_port))
            expect(
                page.get_by_role("button", name="Перейти к скринингам", exact=True)
            ).to_be_visible()
            expect(page.get_by_role("button", name="Занятия", exact=True)).to_have_count(0)
            expect(page.get_by_text("Заниматься дома", exact=True)).to_have_count(0)
            page.get_by_role("button", name="Профили", exact=True).click()
            expect(
                page.get_by_role(
                    "heading", name="Отдельное согласие на обработку данных", exact=True
                )
            ).to_be_visible()
            representative = page.get_by_label(
                "Я совершеннолетний участник или законный представитель ребёнка", exact=True
            )
            marketing = page.get_by_label(
                "Хочу получать предложения и акции сервиса в Telegram (необязательно)", exact=True
            )
            expect(representative).not_to_be_checked()
            expect(marketing).not_to_be_checked()
            representative.check()
            page.get_by_role("button", name="Даю согласие на обработку данных", exact=True).click()
            expect(page.get_by_label("Имя", exact=True)).to_be_visible()
            assert owner.get("/api/privacy").json["accepted"] is True
            page.get_by_role("button", name="Мои данные", exact=True).click()
            page.get_by_role(
                "button", name="Отозвать согласие на обработку данных", exact=True
            ).click()
            expect(page.locator("#notice")).to_contain_text("Согласие отозвано")
            assert owner.get("/api/privacy").json["accepted"] is False
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
