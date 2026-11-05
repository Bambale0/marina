"""Author page 17: the published motor checklist arrives on entering the baby route."""

import copy
import io
import json

import pytest

from marina import Problem
from marina.bot import Bot
from marina.db import connect, transaction
from tests.test_consultation_bot import click
from tests.test_consultation_commerce import app as consultation_app
from tests.test_consultation_remainder import route_setup
from tests.test_funnel import approve_existing, capture, drain
from tests.test_identity import login
from tests.test_screening import publish

app = consultation_app
PDF = b"%PDF-1.4\n% Synthetic non-clinical file fixture\n%%EOF\n"
GIFT = "Synthetic motor checklist attached below."


def setup_material(app, configured=True):
    admin, owner, routes = route_setup(app)
    material = None
    if configured:
        created = admin.post(
            "/api/admin/content",
            json={
                "kind": "material",
                "title": "Synthetic motor checklist fixture",
                "data": {"owner": "Fixture author", "status": "ready", "filename": "motor.pdf"},
                "reason": "Synthetic release regression",
            },
        )
        assert created.status_code == 201, created.json
        draft = created.json
        asset = admin.post(
            "/api/admin/content/" + draft["id"] + "/asset",
            data={
                "reason": "Synthetic PDF upload",
                "file": (io.BytesIO(PDF), "motor.pdf", "application/pdf"),
            },
        )
        assert asset.status_code == 201, asset.json
        edited = admin.patch(
            "/api/admin/content/" + draft["id"],
            json={
                "revision": draft["revision"],
                "reason": "Bind synthetic PDF",
                "data": {**draft["data"], "asset_id": asset.json["id"]},
            },
        )
        assert edited.status_code == 200, edited.json
        material = approve_existing(admin, edited.json)
        config = copy.deepcopy(routes["data"])
        config["routes"]["baby_0_2"].update(
            material_id=material["id"], material_filename="motor.pdf", gift_text=GIFT
        )
        publish(admin, "policy", config)
    sent, files = capture(app)
    app.config["ENFORCE_CONSENT"] = True
    bot = Bot(app, {"id": 2, "first_name": "Parent"}, 2, "outside-entry")
    bot.action("outside_menu", {})
    baby_button = next(
        button
        for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
        for button in row
        if button["text"].startswith("🍼")
    )
    return admin, owner, material, baby_button, sent, files


def download_leads(app):
    with connect(app.config["DATABASE"]) as db:
        return [
            {**dict(row), "data": json.loads(row["data"])}
            for row in db.execute(
                "SELECT * FROM objects WHERE kind='consultation_lead' "
                "AND json_extract(data,'$.action')='download'"
            )
        ]


def notices(sent):
    return [d for _, d in sent if "НЕСТАНДАРТНЫЙ" in d.get("text", "")]


def test_entering_baby_route_attaches_pdf_once_and_keeps_private_lead(app):
    _, owner, _, button, sent, files = setup_material(app)
    click(app, button, 17001)
    assert files == [(2, "motor.pdf", PDF, "application/pdf", "sendDocument")]
    assert any(d.get("text") == GIFT for _, d in sent)
    assert not any(
        b["text"] == "📄 Получить чек-лист"
        for _, d in sent
        for row in d.get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    )
    click(app, button, 17001)  # Duplicate delivery of the same update.
    click(app, button, 17002)  # A second press has a new Telegram update ID.
    drain(app)
    leads = download_leads(app)
    assert len(files) == len(leads) == len(notices(sent)) == 1
    lead = leads[0]
    assert lead["data"]["lead_type"] == "baby_0_2"
    assert notices(sent)[0]["chat_id"] == 1
    path = "/api/consultation-leads/" + lead["id"] + "/pdf"
    assert owner.get(path).data == PDF
    assert login(app, 3).get(path).status_code == 404


def test_entry_resumes_automatic_attachment_after_current_privacy_consent(app):
    admin, owner, _, button, sent, files = setup_material(app)
    publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "New synthetic consent revision",
            "media_max_bytes": 1000000,
            "retention_days": 30,
        },
    )
    assert owner.get("/api/privacy").json["accepted"] is False
    click(app, button, 17101)
    assert not files and not download_leads(app)
    accept = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert accept["text"] == "Прочитал(а) и принимаю"
    click(app, accept, 17102)
    assert owner.get("/api/privacy").json["accepted"] is True
    assert files == [(2, "motor.pdf", PDF, "application/pdf", "sendDocument")]
    click(app, accept, 17103)
    drain(app)
    assert len(files) == len(download_leads(app)) == len(notices(sent)) == 1


@pytest.mark.parametrize("availability", ["empty", "unpublished", "missing_blob"])
def test_entry_does_not_claim_or_deliver_an_unavailable_file(app, availability):
    _, _, material, button, sent, files = setup_material(app, configured=availability != "empty")
    if availability != "empty":
        with transaction(app.config["DATABASE"]) as db:
            if availability == "unpublished":
                db.execute("UPDATE objects SET status='archived' WHERE id=?", (material["id"],))
            else:
                db.execute("DELETE FROM blobs WHERE id=?", (material["data"]["asset_id"],))
    click(app, button, 17201)
    assert files == []
    assert download_leads(app) == []
    assert not any(d.get("text") == GIFT for _, d in sent)
    assert not any(
        b["text"] == "📄 Получить чек-лист"
        for _, d in sent
        for row in d.get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    )


@pytest.mark.parametrize("failed_part", ["file", "gift_text"])
def test_retry_after_delivery_failure_reuses_lead_and_acknowledged_file(app, failed_part):
    _, _, _, button, sent, files = setup_material(app)
    original_send = app.config["HTTP_TRANSPORT"]
    failed = []

    def send_file(*args):
        if failed_part == "file" and not failed:
            failed.append(True)
            raise Problem("Synthetic file transport failure")
        files.append(args)

    def send_message(method, url, data, headers):
        if failed_part == "gift_text" and data.get("text") == GIFT and not failed:
            failed.append(True)
            raise Problem("Synthetic message transport failure")
        return original_send(method, url, data, headers)

    app.config["FILE_TRANSPORT"] = send_file
    app.config["HTTP_TRANSPORT"] = send_message
    click(app, button, 17301)
    assert failed
    assert len(files) == (0 if failed_part == "file" else 1)
    assert len(download_leads(app)) == 1
    drain(app)
    assert len(notices(sent)) == 1
    assert "Запрошен чек-лист" in notices(sent)[0]["text"]
    assert "Получен чек-лист" not in notices(sent)[0]["text"]
    click(app, button, 17302)
    click(app, button, 17303)
    drain(app)
    assert len(files) == len(download_leads(app)) == len(notices(sent)) == 1
    assert len([d for _, d in sent if d.get("text") == GIFT]) == 1
