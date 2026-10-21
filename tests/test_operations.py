from marina.db import connect, transaction, emit
from tests.test_identity import app, login


def test_outbox_restart_retry_and_result_survives(app):
    from marina.operations import work_once

    c = login(app, 2)
    owner = c.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        emit(db, owner, "result-id", "report.published", app.clock[0])

    def fail(*args):
        raise TimeoutError("upstream down")

    assert work_once(app, send=fail) == 1
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "pending"
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    app.clock[0] += 100
    sent = []
    assert work_once(app, send=lambda *args: sent.append(args)) == 1
    assert len(sent) == 1
    assert work_once(app, send=lambda *args: sent.append(args)) == 0


def test_restore_reproduces_account_and_blob(app, tmp_path):
    from marina.operations import backup, verify_backup

    c = login(app, 2)
    owner = c.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                "file",
                owner,
                "subject",
                "case",
                "video/mp4",
                "digest",
                b"private-bytes",
                app.clock[0],
            ),
        )
    path = str(tmp_path / "backup.sqlite")
    backup(app.config["DATABASE"], path)
    assert verify_backup(path)["integrity"] == "ok"
    with connect(path) as db:
        assert db.execute("SELECT body FROM blobs").fetchone()[0] == b"private-bytes"
        assert db.execute("SELECT id FROM accounts").fetchone()[0] == owner


def test_client_never_sees_unapproved_report_or_notes(app):
    from marina.db import insert

    c = login(app, 2)
    owner = c.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        insert(
            db,
            "report",
            {"revisions": [{"text": "SECRET DRAFT"}], "approved_revision": None},
            app.clock[0],
            owner,
        )
        insert(
            db,
            "case",
            {"notes": ["PRIVATE NOTE"], "answer": "Public"},
            app.clock[0],
            owner,
        )
    history = str(c.get("/api/history").json)
    assert "SECRET DRAFT" not in history and "PRIVATE NOTE" not in history


def test_analytics_counts_confirmed_purchase_once_and_hides_payloads(app):
    from tests.test_screening import subject
    from tests.test_commerce import policy, product, quote, pay

    a, c = login(app), login(app, 2)
    policy(a)
    p = product(a)
    q = quote(c, p, [subject(c, "child")]).json
    order = c.post("/api/orders", json={"quote_id": q["id"], "key": "metrics"}).json
    pay(app, order)
    pay(app, order)
    report = a.get(
        "/api/admin/analytics?from=" + str(app.clock[0] - 1) + "&to=" + str(app.clock[0] + 1)
    ).json
    assert report["funnel"]["payment.confirm"] == 1, report
    assert report["receipts"][0]["amount"] == order["data"]["total"]
    assert "answers" not in str(report) and "birth_date" not in str(report)
    assert c.get("/api/admin/analytics").status_code == 403
