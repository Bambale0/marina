"""Real browser UI, Flask/SQLite and bot flow; only Telegram/Lava HTTP is simulated."""

from datetime import datetime, timezone
import itertools
import threading

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from marina.bot import handle_update
from marina.funnel import AGES
from marina.db import connect
from tests.test_identity import app, login, signed
from tests.test_funnel import private_source, scenario, approve_existing, capture, drain
from tests.test_commerce import product
from tests.test_screening import publish


CASES = [
    ("3-4", "НЕЙРОСТАРТ", 3900, 6000),
    ("5-6", "ПЕРВОКЛАССНИК", 3900, 6000),
    ("7-9", "УЧЕНИК", 3900, 6500),
    ("10-14", "ПОДРОСТОК", 4500, 7000),
]


@pytest.fixture
def price_sale(app):
    def prepare(group, code, special, regular):
        app.config.update(
            ENABLED_PAYMENT_PROVIDERS=["lava"],
            TELEGRAM_WEBHOOK_SECRET="fixture",
            LAVA_API_KEY="fixture",
            LAVA_WEBHOOK_SECRET="fixture",
            PUBLIC_URL="https://fixture.test",
        )
        admin, owner = login(app), login(app, 2)
        privacy = publish(
            admin,
            "policy",
            {
                "type": "privacy",
                "terms": "Synthetic browser consent",
                "media_max_bytes": 10000,
                "retention_days": 30,
            },
        )
        assert (
            owner.post(
                "/api/consents", json={"policy_id": privacy["id"], "accepted": True}
            ).status_code
            == 200
        )
        offer = publish(admin, "template", {"type": "offer", "text": "Synthetic purchase terms"})
        publish(
            admin,
            "policy",
            {
                "type": "commerce",
                "scope": "guide",
                "offer_template_id": offer["id"],
                "quote_ttl_seconds": 7200,
                "late_payment": "review",
            },
        )
        publish(
            admin,
            "policy",
            {
                "type": "commerce",
                "scope": "consultation",
                "offer_template_id": offer["id"],
                "quote_ttl_seconds": 900,
                "late_payment": "review",
            },
        )
        consultation = product(
            admin,
            "booking",
            amount=regular * 100,
            currency="RUB",
            age_group=group,
            lava_offer_id="fixture-consultation-offer",
            web_sales_approved=True,
        )
        base = scenario(admin, group)
        material = admin.post(
            f"/api/admin/funnels/{base['id']}/prepare-guide",
            json={
                "revision": base["revision"],
                "reason": "Synthetic browser guide",
            },
        )
        assert material.status_code == 201, material.json
        material = approve_existing(admin, material.json)
        tariff = product(
            admin,
            "guide",
            amount=49000,
            currency="RUB",
            age_group=group,
            material_id=material["id"],
            lava_offer_id="fixture-offer",
            web_sales_approved=True,
        )
        scenario(
            admin,
            group,
            guide_product_id=tariff["id"],
            consultation_product_id=consultation["id"],
            channel_url="https://t.me/fixture",
            contact_url="https://t.me/fixture",
            consultation_code=code,
            consultation_amount_rub=special,
            consultation_regular_rub=regular,
            consultation_valid_days=7,
            consultation_text=f"Synthetic consultation {special} ₽ instead of {regular} ₽. Code {code}.",
            consultation_promo_terms=f"Synthetic coupon {code}: {special} ₽, seven days.",
            offer_text="Synthetic age guide. Стоимость: {price}.",
            followups=[
                {
                    "hours": h,
                    "text": f"Synthetic follow-up {h}. Price {{consultation_price}}. Until {{deadline}}.",
                    "button_label": "Записаться за {consultation_price}",
                    "button_action": "info" if h == 24 else "book",
                }
                for h in (24, 48, 72)
            ],
        )
        sent, files = capture(app)
        upstream = app.config["HTTP_TRANSPORT"]
        invoices = []
        receipt_amount = [490]

        def transport(method, url, data, headers):
            if url == "https://gate.lava.top/api/v3/invoice" and method == "POST":
                invoices.append(data)
                return {
                    "id": "fixture-invoice-" + str(len(invoices)),
                    "paymentUrl": "https://app.lava.top/e2e-invoice",
                }
            if url.startswith("https://gate.lava.top/api/v1/invoices/fixture-invoice-"):
                return {
                    "id": url.rsplit("/", 1)[-1],
                    "status": "completed",
                    "datetime": datetime.fromtimestamp(app.clock[0], timezone.utc).isoformat(),
                    "receipt": {"amount": receipt_amount[0], "currency": "RUB"},
                }
            return upstream(method, url, data, headers)

        app.config["HTTP_TRANSPORT"] = transport
        counter = itertools.count(10000)

        def message(text):
            handle_update(
                app,
                {
                    "update_id": next(counter),
                    "message": {
                        "from": {"id": 2, "first_name": "Buyer"},
                        "chat": {"id": 2, "type": "private"},
                        "text": text,
                    },
                },
            )

        def click(label):
            button = next(
                b
                for _, payload in reversed(sent)
                for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
                for b in row
                if b["text"] == label
            )
            handle_update(
                app,
                {
                    "update_id": next(counter),
                    "callback_query": {
                        "id": "fixture",
                        "from": {"id": 2, "first_name": "Buyer"},
                        "data": button["callback_data"],
                        "message": {"chat": {"id": 2, "type": "private"}},
                    },
                },
            )

        return admin, owner, sent, files, invoices, receipt_amount, message, click

    return prepare


@pytest.fixture
def browser_page(app, tmp_path):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        context.route(
            "https://app.lava.top/e2e-invoice",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<h1>Synthetic Lava checkout</h1>"
            ),
        )
        page = context.new_page()
        page.base_url = f"http://127.0.0.1:{server.server_port}"
        page.errors = []
        page.on("pageerror", lambda error: page.errors.append(str(error)))
        page.route("https://telegram.org/**", lambda route: route.abort())
        page.add_init_script(
            "window.Telegram={WebApp:{initData:window.name,ready(){},expand(){}}};"
        )

        def open_as(user_id, path="/"):
            init_data = signed(user_id, app.clock[0], query_id=f"browser-price-{user_id}")
            page.evaluate(
                "value => {window.name=value; if(location.protocol==='http:') sessionStorage.clear();}",
                init_data,
            )
            page.goto(page.base_url + path)
            expect(page.get_by_role("button", name="Мои обращения", exact=True)).to_be_visible()

        page.open_as = open_as
        try:
            yield page
            assert not page.errors, page.errors
        finally:
            context.tracing.stop(path=str(tmp_path / "price-e2e-trace.zip"))
            context.close()
            browser.close()
            server.shutdown()
            thread.join(timeout=3)


def test_admin_tariff_displays_rubles_not_kopecks(app, price_sale, browser_page):
    price_sale(*CASES[0])
    page = browser_page
    page.open_as(1, "/admin")
    card = page.locator("article").filter(
        has=page.get_by_role("heading", name=AGES["3-4"], exact=True)
    )
    expect(card.get_by_text("490.00 ₽ · Lava offer настроен", exact=True)).to_be_visible()
    expect(card.get_by_text("49000 RUB", exact=False)).to_have_count(0)


@pytest.mark.parametrize("group,code,special,regular", CASES)
def test_browser_and_bot_price_to_payment_pdf_and_consultation(
    app, price_sale, browser_page, group, code, special, regular
):
    admin, owner, sent, files, invoices, receipt_amount, message, click = price_sale(
        group, code, special, regular
    )
    page = browser_page
    page.open_as(2)
    message("/start")
    click(AGES[group])
    for _ in range(6):
        assert "[Вопрос" not in sent[-1][1]["text"]
        click("Б")
    drain(app)
    app.clock[0] += 30
    drain(app)
    app.clock[0] += 180
    drain(app)
    assert "490,00 ₽" in sent[-1][1]["text"]
    click("💳 Забрать руководство за 490,00 ₽")
    message("buyer@example.test")
    drain(app)
    assert len(invoices) == 1 and invoices[0]["amount"] == 490 and invoices[0]["currency"] == "RUB"
    page.get_by_role("button", name="Мои обращения", exact=True).click()
    order_card = page.locator("article").filter(
        has=page.get_by_role("heading", name="Заказ", exact=True)
    )
    order_card.get_by_role("button", name="Открыть", exact=True).click()
    expect(page.get_by_role("heading", name="490.00 ₽", exact=True)).to_be_visible()
    with page.expect_popup() as popup:
        page.get_by_role("button", name="Оплатить", exact=True).click()
    popup.value.wait_for_url("https://app.lava.top/e2e-invoice")
    popup.value.close()
    auth = {"X-Api-Key": "fixture"}
    payload = {"eventType": "payment.success", "contractId": "fixture-invoice-1"}
    for _ in range(2):
        response = page.request.post(
            page.base_url + "/api/webhooks/lava", headers=auth, data=payload
        )
        assert response.status == 200
        drain(app)
    assert len(files) == 1 and files[0][2].startswith(b"%PDF")
    orders = admin.get("/api/admin/objects?kind=order").json["items"]
    order = orders[0]
    assert (
        order["status"] == "paid"
        and order["data"]["total"] == 49000
        and order["data"]["currency"] == "RUB"
    )
    if group == "3-4":
        page.open_as(1, "/admin")
        page.get_by_role("button", name="Все объекты", exact=True).click()
        page.get_by_label("Тип объекта", exact=True).select_option("order")
        page.locator("article").filter(
            has=page.get_by_role("heading", name="Заказ", exact=True)
        ).get_by_role("button", name="Открыть карточку", exact=True).click()
        expect(
            page.get_by_label("Подтверждённое поступление для полного возврата")
        ).to_contain_text("lava · 490.00 ₽")
        expect(
            page.get_by_role("button", name="Запросить полный возврат Stars", exact=True)
        ).to_have_count(0)
    for hours in (24, 48, 72):
        app.clock[0] += 86400
        drain(app)
        assert f"{special} ₽" in sent[-1][1]["text"]
        if hours == 24:
            click(f"Записаться за {special} ₽")
            assert f"{special} ₽" in sent[-1][1]["text"] and code in sent[-1][1]["text"]
    click(f"Записаться за {special} ₽")
    message("Завтра после 18:00 МСК")
    booking = next(o for o in owner.get("/api/history").json["items"] if o["kind"] == "booking")
    assert booking["data"]["promotion"]["eligible"] is True
    assert booking["data"]["promotion"]["amount_rub"] == special
    assert booking["data"]["promotion"]["regular_amount_rub"] == regular
    assert booking["data"]["promotion"]["code"] == code
    proposal = admin.post(
        f"/api/admin/bookings/{booking['id']}/propose",
        json={
            "revision": booking["revision"],
            "reason": "Synthetic appointment",
            "starts": app.clock[0] + 86400,
            "duration_minutes": 60,
            "timezone": "Europe/Moscow",
        },
    )
    assert proposal.status_code == 200, proposal.json
    drain(app)
    click("Подтвердить время")
    assert f"{special},00 ₽" in sent[-1][1]["text"]
    click(f"💳 Оплатить консультацию за {special},00 ₽")
    message("buyer@example.test")
    drain(app)
    assert len(invoices) == 2 and invoices[1]["amount"] == special
    assert invoices[1]["offerId"] == "fixture-consultation-offer"
    receipt_amount[0] = special
    payload["contractId"] = "fixture-invoice-2"
    for _ in range(2):
        response = page.request.post(
            page.base_url + "/api/webhooks/lava", headers=auth, data=payload
        )
        assert response.status == 200
        drain(app)
    paid_booking = owner.get(f"/api/bookings/{booking['id']}").json
    assert paid_booking["data"]["paid_at"] == app.clock[0]
    assert (
        sum("Консультация оплачена" in p.get("text", "") for _, p in sent if p.get("chat_id") == 2)
        == 1
    )
    assert len(files) == 1
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM fulfillments").fetchone()[0] == 2
