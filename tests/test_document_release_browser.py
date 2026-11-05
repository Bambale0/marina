"""Real web controls for a personal result; transport only is simulated."""

import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from tests.test_consultation_commerce import app
from tests.test_consultation_results import completed_consultation, result_pdf
from tests.test_funnel import drain


def object_list_response(response, kind="", status=""):
    url = urlsplit(response.url)
    query = parse_qs(url.query, keep_blank_values=True)
    return (
        response.status == 200
        and response.request.method == "GET"
        and url.path == "/api/admin/objects"
        and query.get("kind") == [kind]
        and query.get("status") == [status]
        and query.get("offset") == ["0"]
    )


def wait_object_list(page, response):
    response.finished()
    listing = page.locator("#main [data-object-list]")
    expect(listing).to_have_attribute("aria-busy", "false")
    items = response.json()["items"]
    expect(listing.locator("article[data-object-id]")).to_have_count(len(items))
    for item in items:
        expect(listing.locator('article[data-object-id="' + item["id"] + '"]')).to_have_count(1)


def test_personal_pdf_web_publication_download_and_nullable_teen_deadline(app):
    admin, owner, booking, result, _, _ = completed_consultation(app, "10-14", 700000, 4500)
    active = next(
        item
        for item in admin.get("/api/admin/content?kind=funnel&order=desc").json["items"]
        if item["status"] == "published"
        and item["data"].get("age_group") == "10-14"
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
            with page.expect_response(object_list_response) as objects:
                page.get_by_role("button", name="Все объекты", exact=True).click()
            wait_object_list(page, objects.value)
            with page.expect_response(lambda r: object_list_response(r, "funnel")) as funnels:
                page.get_by_label("Тип объекта", exact=True).select_option("funnel")
            wait_object_list(page, funnels.value)
            with page.expect_response(
                lambda r: object_list_response(r, "funnel", "draft")
            ) as drafts:
                page.get_by_label("Состояние", exact=True).select_option("draft")
            wait_object_list(page, drafts.value)
            draft_card = page.locator('article[data-object-id="' + draft["id"] + '"]')
            expect(draft_card).to_have_count(1)
            draft_card.get_by_role("button", name="Открыть карточку", exact=True).click()
            deadline = page.get_by_label(
                "Срок результата, часов (пусто — срок не указан в источнике)", exact=True
            )
            expect(deadline).to_have_value("")
            page.get_by_label("Основание / причина действия", exact=True).fill(
                "Preserve unspecified teenage deadline"
            )
            with page.expect_response(
                lambda r: r.url.endswith("/api/admin/content/" + draft["id"])
                and r.request.method == "PATCH"
                and r.status == 200
            ) as saved:
                page.get_by_role("button", name="Сохранить черновик", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("Черновик сохранён")
            expect(
                page.get_by_text(
                    "ID: " + draft["id"] + " · редакция " + str(saved.value.json()["revision"]),
                    exact=True,
                )
            ).to_be_visible()
            stored = admin.get("/api/admin/content/" + draft["id"]).json
            assert stored["data"]["consultation_journey"]["result_delivery"]["due_hours"] is None
            page.get_by_label("Основание / причина действия", exact=True).fill(
                "Review complete configuration"
            )
            with page.expect_response(
                lambda r: r.url.endswith("/" + draft["id"] + "/approve") and r.status == 200
            ) as approved:
                page.get_by_role("button", name="Утвердить эту редакцию", exact=True).click()
            expect(
                page.get_by_text(
                    "ID: " + draft["id"] + " · редакция " + str(approved.value.json()["revision"]),
                    exact=True,
                )
            ).to_be_visible()
            expect(page.get_by_role("button", name="Опубликовать", exact=True)).to_be_visible()

            with page.expect_response(object_list_response) as objects:
                page.get_by_role("button", name="Все объекты", exact=True).click()
            wait_object_list(page, objects.value)
            with page.expect_response(
                lambda r: object_list_response(r, "consultation_result")
            ) as results:
                page.get_by_label("Тип объекта", exact=True).select_option("consultation_result")
            wait_object_list(page, results.value)
            result_card = page.locator('article[data-object-id="' + result["id"] + '"]')
            expect(result_card).to_have_count(1)
            result_card.get_by_role("button", name="Открыть карточку", exact=True).click()
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
            expect(
                page.get_by_text(
                    "PDF опубликован. Доставка в Telegram ожидает подтверждения.", exact=True
                )
            ).to_be_visible()
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


def test_admin_object_filters_ignore_late_response_from_previous_selection(app):
    admin, _, _, result, _, _ = completed_consultation(app, "10-14", 700000, 4500)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            token = admin.environ_base["HTTP_AUTHORIZATION"].removeprefix("Bearer ")
            page.add_init_script(
                "sessionStorage.setItem('marina-token', " + json.dumps(token) + ");"
            )
            page.route("https://telegram.org/**", lambda route: route.abort())
            page.goto("http://127.0.0.1:" + str(server.server_port) + "/admin")
            with page.expect_response(object_list_response) as initial:
                page.get_by_role("button", name="Все объекты", exact=True).click()
            wait_object_list(page, initial.value)

            held = []

            def delay_funnel_response(route):
                query = parse_qs(urlsplit(route.request.url).query, keep_blank_values=True)
                if query.get("kind") == ["funnel"]:
                    held.append((route, route.fetch()))
                else:
                    route.continue_()

            page.route("**/api/admin/objects?*", delay_funnel_response)
            page.get_by_label("Тип объекта", exact=True).select_option("funnel")
            with page.expect_response(
                lambda r: object_list_response(r, "consultation_result")
            ) as newest:
                page.get_by_label("Тип объекта", exact=True).select_option("consultation_result")
            wait_object_list(page, newest.value)
            assert [item["id"] for item in newest.value.json()["items"]] == [result["id"]]
            assert len(held) == 1
            route, stale = held[0]
            assert all(item["kind"] == "funnel" for item in stale.json()["items"])
            assert stale.json()["items"]
            with page.expect_response(lambda r: object_list_response(r, "funnel")) as delayed:
                route.fulfill(response=stale)
            delayed.value.finished()
            # Let the fulfilled HTTP response and the following paint complete; no app internals.
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            expect(page.get_by_label("Тип объекта", exact=True)).to_have_value(
                "consultation_result"
            )
            wait_object_list(page, newest.value)
            result_card = page.locator('article[data-object-id="' + result["id"] + '"]')
            expect(result_card).to_be_visible()
            expect(
                result_card.get_by_role("button", name="Открыть карточку", exact=True)
            ).to_be_visible()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
