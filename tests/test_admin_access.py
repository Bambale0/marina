"""Admin access uses real SQLite fulfillment without payment evidence."""

from marina.db import connect, transaction
from tests.test_identity import app, login
from tests.test_screening import publish, subject
from tests.test_commerce import policy, product, quote
from tests.test_funnel import setup_sale, start, finish, drain
from tests.test_training import exercises
from tests.funnel_fixtures import configure_funnel_source


def test_admin_order_fulfills_once_without_payment_or_receipt(app):
    admin = login(app)
    policy(admin)
    tariff = product(admin)
    child = subject(admin, "child")
    calculation = quote(admin, tariff, [child]).json
    payload = {"quote_id": calculation["id"], "key": "admin-buy"}
    first = admin.post("/api/orders", json=payload)
    assert first.status_code == 200, first.json
    assert first.json["status"] == "paid"
    assert first.json["data"]["access_source"] == "admin"
    assert admin.post("/api/orders", json=payload).json["id"] == first.json["id"]
    assert (
        admin.post("/api/orders/" + first.json["id"] + "/checkout", json={}).json["status"]
        == "paid"
    )
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM fulfillments").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='payment_attempt'").fetchone()[0]
            == 0
        )


def test_client_cannot_request_admin_access(app):
    admin, client = login(app), login(app, 2)
    policy(admin)
    tariff = product(admin)
    child = subject(client, "child")
    calculation = quote(client, tariff, [child]).json
    response = client.post(
        "/api/orders",
        json={
            "quote_id": calculation["id"],
            "key": "client-buy",
            "access_source": "admin",
            "role": "admin",
        },
    )
    assert response.json["status"] == "pending"
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM fulfillments").fetchone()[0] == 0


def test_admin_guide_works_without_lava_and_after_offer_expiry(app, tmp_path):
    configure_funnel_source(app, tmp_path)
    app.config.update(
        DEV_EXTERNAL_LAVA_CHECKOUT=True,
        LAVA_API_KEY="fixture",
        LAVA_WEBHOOK_SECRET="fixture",
        PUBLIC_URL="https://fixture.test",
    )
    admin, client, _, _, _, sent, files = setup_sale(
        app, currency="RUB", lava_offer_id="fixture-offer", web_sales_approved=True
    )
    run = finish(admin, start(admin, "3-4"), ["b"] * 6)
    drain(app)
    app.clock[0] += 30
    drain(app)
    app.clock[0] += 8000
    app.config.update(LAVA_API_KEY="", LAVA_WEBHOOK_SECRET="", DEV_EXTERNAL_LAVA_CHECKOUT=False)
    response = admin.post("/api/funnel-runs/" + run["id"] + "/purchase", json={})
    assert response.status_code == 200, response.json
    order = response.json
    assert order["status"] == "paid"
    assert (
        admin.post("/api/funnel-runs/" + run["id"] + "/purchase", json={}).json["id"] == order["id"]
    )
    drain(app)
    rights = [o for o in admin.get("/api/history").json["items"] if o["kind"] == "right"]
    assert len(rights) == 1
    pdf = admin.get("/api/guides/" + rights[0]["id"] + "/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    assert client.get("/api/guides/" + rights[0]["id"] + "/pdf").status_code == 404
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM objects WHERE kind='payment_attempt'").fetchone()[0]
            == 0
        )


def test_admin_training_remains_available_without_trial_or_subscription(app):
    admin = login(app)
    child = subject(admin, "child")
    exercises(app, admin)
    for day in range(2):
        app.clock[0] += day * 4 * 86400
        response = admin.post(
            "/api/lessons/start",
            json={"subject_id": child["id"], "equipment": [], "constraints": [], "key": str(day)},
        )
        assert response.status_code == 200, response.json
        assert admin.get("/api/lessons/" + response.json["id"]).status_code == 200
    assert admin.get("/api/me").json["trial"] is None


def test_admin_can_open_course_lessons_without_purchase_or_prerequisites(app):
    admin, client = login(app), login(app, 2)
    own, other = subject(admin), subject(client)
    course = publish(
        admin,
        "course",
        {
            "source": "Synthetic fixture",
            "instructions": "Program",
            "rights": "test",
            "audience": "adult",
            "min_months": 216,
            "max_months": 1440,
            "lessons": [
                {"id": "first", "title": "First", "text": "First text"},
                {"id": "second", "title": "Second", "text": "Second text", "requires": ["first"]},
            ],
        },
    )
    path = "/api/courses/" + course["id"] + "/lessons/second?subject_id="
    assert admin.get(path + own["id"]).status_code == 200
    assert client.get(path + other["id"]).status_code == 403
    assert admin.get(path + other["id"]).status_code == 404


def test_admin_bot_buy_keeps_confirmation_flow_without_invoice(app):
    from marina.bot import Bot
    from tests.test_funnel import capture

    admin = login(app)
    policy(admin)
    tariff = product(admin)
    child = subject(admin, "child")
    calculation = quote(admin, tariff, [child]).json
    sent, files = capture(app)
    bot = Bot(app, {"id": 1, "first_name": "Admin"}, 1, "admin-buy")
    bot.action("buy", {"id": calculation["id"]})
    assert sent
    assert not any(method in ("sendInvoice", "createInvoiceLink") for method, _ in sent)
    assert sent[-1][1]["text"] == "Доступ открыт."


def test_admin_downloads_existing_report_without_own_purchase(app):
    from marina.db import insert

    admin, owner, stranger = login(app), login(app, 2), login(app, 3)
    owner_id = owner.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                "report-pdf",
                owner_id,
                "",
                "",
                "application/pdf",
                "fixture",
                b"%PDF-fixture",
                app.clock[0],
            ),
        )
        report = insert(
            db, "report", {"pdf_id": "report-pdf"}, app.clock[0], owner_id, status="published"
        )
    path = "/api/reports/" + report["id"] + "/pdf"
    assert admin.get(path).data == b"%PDF-fixture"
    assert owner.get(path).status_code == 200
    assert stranger.get(path).status_code == 404
