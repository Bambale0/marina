"""Real web controls for a personal result; transport only is simulated."""

import json
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from tests.test_consultation_commerce import app
from tests.test_consultation_results import completed_consultation, result_pdf
from tests.test_funnel import drain


def test_personal_pdf_web_publication_download_and_nullable_teen_deadline(app):
    admin, owner, booking, result, _, _ = completed_consultation(app, "10-14", 700000, 4500)
    active = next(
        item
        for item in admin.get("/api/admin/content?kind=funnel&order=desc").json["items"]
        if item["status"] == "published"
        and item["data"].get("consultation_journey", {}).get("result_delivery")
    )
    draft = admin.post(
        "/api/admin/content/" + active["id"] + "/copy",
        json={"revision": active["revision"], "reason": "Check nullable author deadline"},
    )
    assert draft.status_code == 200, draft.json
    draft = draft.json
    raw = result_pdf()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            errors = []

            def page_for(client, path):
                page = browser.new_page()
                token = client.environ_base["HTTP_AUTHORIZATION"].removeprefix("Bearer ")
                page.add_init_script(
                    "sessionStorage.setItem('marina-token', " + json.dumps(token) + ");"
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("https://telegram.org/**", lambda route: route.abort())
                page.goto("http://127.0.0.1:" + str(server.server_port) + path)
                return page

            page = page_for(admin, "/admin")
            page.get_by_role("button", name="Все объекты", exact=True).click()
            page.get_by_label("Тип объекта", exact=True).select_option("funnel")
            page.get_by_label("Состояние", exact=True).select_option("draft")
            page.get_by_role("button", name="Открыть карточку", exact=True).click()
            deadline = page.get_by_label(
                "Срок результата, часов (пусто — срок не указан в источнике)", exact=True
            )
            expect(deadline).to_have_value("")
            page.get_by_label("Основание / причина действия", exact=True).fill(
                "Preserve unspecified teenage deadline"
            )
            page.get_by_role("button", name="Сохранить черновик", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("Черновик сохранён")
            stored = admin.get("/api/admin/content/" + draft["id"]).json
            assert stored["data"]["consultation_journey"]["result_delivery"]["due_hours"] is None
            page.get_by_label("Основание / причина действия", exact=True).fill(
                "Review complete configuration"
            )
            with page.expect_response(
                lambda r: r.url.endswith("/" + draft["id"] + "/approve") and r.status == 200
            ):
                page.get_by_role("button", name="Утвердить эту редакцию", exact=True).click()

            page.get_by_role("button", name="Все объекты", exact=True).click()
            page.get_by_label("Тип объекта", exact=True).select_option("consultation_result")
            page.get_by_role("button", name="Открыть карточку", exact=True).click()
            expect(
                page.get_by_text("Срок отправки не указан в авторском документе.", exact=True)
            ).to_be_visible()
            page.get_by_label("Причина действия", exact=True).fill(
                "Specialist reviewed this personal PDF"
            )
            page.get_by_label("Персональный PDF специалиста", exact=True).set_input_files(
                {"name": "personal.pdf", "mimeType": "application/pdf", "buffer": raw}
            )
            with page.expect_response(
                lambda r: r.url.endswith("/" + result["id"] + "/media") and r.status == 201
            ):
                page.get_by_role("button", name="Загрузить персональный PDF", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("PDF сохранён")
            assert (
                owner.get("/api/consultation-results/" + result["id"] + "/pdf").status_code == 404
            )
            with page.expect_response(
                lambda r: r.url.endswith("/" + result["id"] + "/publish") and r.status == 200
            ):
                page.get_by_role("button", name="Опубликовать персональный PDF", exact=True).click()
            assert (
                owner.get("/api/consultation-results/" + result["id"]).json["status"]
                == "delivery_pending"
            )
            drain(app)
            assert (
                owner.get("/api/consultation-results/" + result["id"]).json["status"] == "completed"
            )
            page.get_by_role("button", name="Открыть запись на консультацию", exact=True).click()
            expect(page.get_by_label("Длительность, минут", exact=True)).to_have_value("75")
            page.get_by_role("button", name="Открыть персональный результат", exact=True).click()
            expect(page.get_by_text("Доставлен в Telegram:", exact=False)).to_be_visible()

            client_page = page_for(owner, "/")
            client_page.get_by_role("button", name="Мои обращения", exact=True).click()
            result_card = client_page.locator("article").filter(
                has=client_page.get_by_role(
                    "heading", name="Персональный результат консультации", exact=True
                )
            )
            result_card.get_by_role("button", name="Открыть", exact=True).click()
            with client_page.expect_download() as downloaded:
                client_page.get_by_role(
                    "button", name="Скачать персональный PDF", exact=True
                ).click()
            assert downloaded.value.suggested_filename == result["data"]["filename"]
            assert Path(downloaded.value.path()).read_bytes() == raw
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
