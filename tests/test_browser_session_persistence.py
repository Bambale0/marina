"""Browser login survives closed tabs, while logout clears persisted access."""

import hashlib
import json
import threading

import pytest
from werkzeug.serving import make_server

from marina.db import transaction
from tests.funnel_fixtures import configure_funnel_source
from tests.test_identity import app, login


@pytest.fixture
def browser_server(app, tmp_path):
    configure_funnel_source(app, tmp_path)
    admin = login(app)
    owner = admin.get("/api/me").json["account"]["id"]
    code = "browser-session-persistence-code-unique"
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO login_codes VALUES(?,?,?)",
            (hashlib.sha256(code.encode()).hexdigest(), owner, app.clock[0] + 300),
        )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", code, admin
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_browser_closed_tab_keeps_session_and_logout_clears_it(browser_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    base, code, _ = browser_server
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.route("https://telegram.org/**", lambda route: route.abort())
        page = context.new_page()
        page.goto(base + "/admin")
        page.get_by_label("Одноразовый код из команды /login в боте").fill(code)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_role("heading", name="Трипваер", exact=True).wait_for()
        token = page.evaluate('localStorage.getItem("marina-token")')
        assert token
        page.close()

        restored = context.new_page()
        with restored.expect_response(lambda r: r.url == base + "/api/me") as response:
            restored.goto(base + "/admin")
        assert response.value.status == 200
        restored.get_by_role("heading", name="Трипваер", exact=True).wait_for()
        assert restored.get_by_role("button", name="Войти", exact=True).count() == 0
        assert restored.evaluate('localStorage.getItem("marina-token")') == token

        restored.get_by_role("button", name="Выйти", exact=True).click()
        restored.get_by_role("button", name="Войти", exact=True).wait_for()
        assert restored.evaluate('localStorage.getItem("marina-token")') is None
        restored.close()
        logged_out = context.new_page()
        logged_out.goto(base + "/admin")
        logged_out.get_by_role("button", name="Войти", exact=True).wait_for()
        assert (
            logged_out.evaluate(
                """async (token) => (await fetch("/api/me", {
                headers: {Authorization: "Bearer " + token}
            })).status""",
                token,
            )
            == 401
        )
        browser.close()


def test_browser_migrates_old_tab_token_before_tab_closes(browser_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    base, _, admin = browser_server
    token = admin.environ_base["HTTP_AUTHORIZATION"].removeprefix("Bearer ")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.route("https://telegram.org/**", lambda route: route.abort())
        old_tab = context.new_page()
        old_tab.add_init_script(
            "sessionStorage.setItem('marina-token', " + json.dumps(token) + ");"
        )
        old_tab.goto(base + "/admin")
        old_tab.get_by_role("heading", name="Трипваер", exact=True).wait_for()
        assert old_tab.evaluate('localStorage.getItem("marina-token")') == token
        assert old_tab.evaluate('sessionStorage.getItem("marina-token")') is None
        old_tab.close()
        restored = context.new_page()
        with restored.expect_response(lambda r: r.url == base + "/api/me") as response:
            restored.goto(base + "/admin")
        assert response.value.status == 200
        restored.get_by_role("heading", name="Трипваер", exact=True).wait_for()
        browser.close()


@pytest.mark.parametrize("has_account_binding", [True, False])
def test_browser_switching_telegram_account_drops_previous_user_session(
    app, browser_server, has_account_binding
):
    from tests.test_identity import signed

    sync_api = pytest.importorskip("playwright.sync_api")
    base, code, _ = browser_server
    init_data = signed(2, app.clock[0], query_id="browser-account-switch")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.route("https://telegram.org/**", lambda route: route.abort())
        admin_page = context.new_page()
        admin_page.goto(base + "/admin")
        admin_page.get_by_label("Одноразовый код из команды /login в боте").fill(code)
        admin_page.get_by_role("button", name="Войти", exact=True).click()
        admin_page.get_by_role("heading", name="Трипваер", exact=True).wait_for()
        admin_token = admin_page.evaluate('localStorage.getItem("marina-token")')
        assert admin_token
        if not has_account_binding:
            admin_page.evaluate('localStorage.removeItem("marina-user")')
        admin_page.close()

        next_account = context.new_page()
        next_account.add_init_script(
            "window.Telegram = {WebApp: {initData: "
            + json.dumps(init_data)
            + ", initDataUnsafe: {user: {id: 2}}, ready() {}, expand() {}}};"
        )
        with next_account.expect_response(lambda r: r.url == base + "/api/me") as response:
            next_account.goto(base + "/")
        assert response.value.status == 200
        assert response.value.json()["account"]["telegram_id"] == 2
        assert next_account.evaluate('localStorage.getItem("marina-token")') != admin_token
        assert next_account.evaluate('localStorage.getItem("marina-user")') == "2"
        browser.close()
