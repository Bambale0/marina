"""Specialist publication and private client downloads through actual Telegram controls."""

import io
from itertools import count

import pytest

from marina import object_get
from marina.bot import Bot, handle_update
from marina.db import connect, transaction, update
from tests.test_consultation_commerce import app, confirmed_consultation, pay_consultation, quote
from tests.test_consultation_remainder import make_response_audio, task_setup
from tests.test_consultation_results import completed_consultation, result_pdf
from tests.test_crm import make_video
from tests.test_funnel import drain


class Telegram:
    def __init__(self, app):
        self.app, self.calls, self.files, self.uploads = app, [], [], {}
        self.ids = count(70000)
        app.config["HTTP_TRANSPORT"] = self.transport
        app.config["FILE_TRANSPORT"] = lambda *args: self.files.append(args)
        app.config["TELEGRAM_FILE_TRANSPORT"] = lambda path, limit: self.uploads[path]

    def transport(self, method, url, data, headers):
        self.calls.append((url.rsplit("/", 1)[-1], data))
        result = (
            {"file_path": "fixtures/" + data["file_id"]}
            if url.endswith("/getFile")
            else {"message_id": len(self.calls)}
        )
        return {"ok": True, "result": result}

    def bot(self, user):
        return Bot(self.app, {"id": user, "first_name": "Synthetic"}, user, next(self.ids))

    def message(self, user, text=None, **fields):
        handle_update(
            self.app,
            {
                "update_id": next(self.ids),
                "message": {
                    "from": {"id": user, "first_name": "Synthetic"},
                    "chat": {"id": user, "type": "private"},
                    **({"text": text} if text is not None else {}),
                    **fields,
                },
            },
        )

    def button(self, title, user=1):
        for endpoint, message in reversed(self.calls):
            if endpoint == "sendMessage" and message.get("chat_id") == user:
                for row in message.get("reply_markup", {}).get("inline_keyboard", []):
                    for button in row:
                        if button["text"] == title:
                            return button
        raise AssertionError("Missing Telegram button: " + title)

    def click(self, button, user=1):
        sequence = next(self.ids)
        handle_update(
            self.app,
            {
                "update_id": sequence,
                "callback_query": {
                    "id": str(sequence),
                    "from": {"id": user, "first_name": "Synthetic"},
                    "data": button["callback_data"],
                    "message": {"chat": {"id": user, "type": "private"}},
                },
            },
        )

    def upload(self, raw, mime, message_type="document"):
        key = "upload-" + str(next(self.ids))
        self.uploads["fixtures/" + key] = raw
        extension = {"application/pdf": "pdf", "audio/ogg": "ogg", "audio/mpeg": "mp3"}[mime]
        self.message(
            1,
            **{
                message_type: {
                    "file_id": key,
                    "file_size": len(raw),
                    "mime_type": mime,
                    "file_name": "specialist." + extension,
                }
            },
        )


def submitted_task(app, tmp_path):
    admin, owner, task = task_setup(app)
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Synthetic specialist assignment",
            "reason": "Specialist assigns three tasks",
        },
    )
    assert assigned.status_code == 200, assigned.json
    raw = make_video(tmp_path)
    for slot in range(1, 4):
        uploaded = owner.post(
            "/api/video-tasks/" + task["id"] + "/video",
            data={"slot": str(slot), "file": (io.BytesIO(raw), "task.mp4", "video/mp4")},
        )
        assert uploaded.status_code == 200, uploaded.json
    submitted = owner.post("/api/video-tasks/" + task["id"] + "/submit", json={})
    assert submitted.status_code == 200, submitted.json
    drain(app)
    return admin, owner, submitted.json


def test_telegram_specialist_pdf_upload_explicit_publication_and_owner_redownload(app):
    _, owner, booking, result, _, _ = completed_consultation(app)
    telegram = Telegram(app)
    telegram.bot(1).admin_card(result["id"])
    telegram.click(telegram.button("Загрузить персональный PDF"))
    telegram.message(1, "Специалист подготовил и проверил персональный результат")
    raw = result_pdf()
    telegram.upload(raw, "application/pdf")
    pending = telegram.bot(1).state["data"]["admin_pending"]
    assert owner.get("/api/media/" + pending["attachment_id"]).status_code == 404
    assert owner.get("/api/consultation-results/" + result["id"]).json["status"] == "in_progress"
    assert not telegram.files
    publish = telegram.button("Опубликовать персональный PDF")
    telegram.click(publish)
    assert (
        owner.get("/api/consultation-results/" + result["id"]).json["status"] == "delivery_pending"
    )
    drain(app)
    assert telegram.files[-1][0] == 2 and telegram.files[-1][2] == raw
    telegram.bot(2).card(booking["id"], "booking")
    telegram.click(telegram.button("Открыть персональный результат", 2), 2)
    telegram.click(telegram.button("Скачать персональный PDF", 2), 2)
    assert telegram.files[-1][0] == 2 and telegram.files[-1][2] == raw
    before = len(telegram.files)
    telegram.click(telegram.button("Скачать персональный PDF", 2), 3)
    assert len(telegram.files) == before
    telegram.bot(2).action("history", {})
    assert any(
        "Персональный результат" in b["text"]
        for endpoint, m in telegram.calls
        if endpoint == "sendMessage" and m.get("chat_id") == 2
        for row in m.get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    )
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE event='consultation_result.published'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "message_type,extension,mime,method",
    [
        ("voice", "ogg", "audio/ogg", "sendVoice"),
        ("audio", "mp3", "audio/mpeg", "sendAudio"),
        ("document", "mp3", "audio/mpeg", "sendAudio"),
    ],
)
def test_telegram_specialist_audio_flow_keeps_draft_private_and_client_can_download(
    app, tmp_path, message_type, extension, mime, method
):
    _, owner, task = submitted_task(app, tmp_path)
    telegram = Telegram(app)
    telegram.bot(1).admin_card(task["id"])
    telegram.click(telegram.button("Подготовить аудиоразбор"))
    telegram.message(1, "Письменное сопровождение аудиоразбора специалиста")
    telegram.message(1, "Личный разбор выполненных заданий специалистом")
    raw = make_response_audio(tmp_path, extension)
    telegram.upload(raw, mime, message_type)
    pending = telegram.bot(1).state["data"]["admin_pending"]
    assert owner.get("/api/media/" + pending["attachment_id"]).status_code == 404
    assert owner.get("/api/video-tasks/" + task["id"]).json["status"] == "submitted"
    telegram.click(telegram.button("Опубликовать аудиоразбор"))
    completed = owner.get("/api/video-tasks/" + task["id"]).json
    assert completed["status"] == "completed"
    assert completed["data"]["answer_attachments"] == [pending["attachment_id"]]
    drain(app)
    telegram.bot(2).card(task["id"], "video_task")
    telegram.click(telegram.button("Скачать материалы разбора", 2), 2)
    assert telegram.files[-1][0] == 2
    assert telegram.files[-1][2] == raw and telegram.files[-1][4] == method


def test_cancelled_specialist_prompt_cannot_publish_stale_uploaded_pdf(app):
    _, owner, _, result, _, _ = completed_consultation(app)
    telegram = Telegram(app)
    telegram.bot(1).admin_card(result["id"])
    telegram.click(telegram.button("Загрузить персональный PDF"))
    telegram.message(1, "Подготовлен черновик для проверки")
    telegram.upload(result_pdf(), "application/pdf")
    stale = telegram.button("Опубликовать персональный PDF")
    media_id = telegram.bot(1).state["data"]["admin_pending"]["attachment_id"]
    telegram.message(1, "/admin")
    telegram.click(stale)
    assert owner.get("/api/consultation-results/" + result["id"]).json["status"] == "in_progress"
    assert owner.get("/api/media/" + media_id).status_code == 404


def test_specialist_can_record_manual_payment_without_inventing_provider_receipt(app):
    _, owner, task = task_setup(app)
    telegram = Telegram(app)
    telegram.bot(1).admin_card(task["id"])
    telegram.click(telegram.button("Подтвердить оплату"))
    telegram.message(1, "Synthetic bank transfer reference 123")
    telegram.message(1, "Специалист проверил фактическое поступление")
    saved = owner.get("/api/video-tasks/" + task["id"]).json
    assert saved["data"]["manual_payment"]["status"] == "confirmed"
    assert saved["data"]["manual_payment"]["amount_rub"] == task["data"]["amount_rub"]
    assert "reference" not in saved["data"]["manual_payment"]
    assert saved["status"] == task["status"]
    telegram.bot(2).card(task["id"], "video_task")
    assert any(
        "Оплата подтверждена специалистом" in message.get("text", "")
        for endpoint, message in telegram.calls
        if endpoint == "sendMessage" and message.get("chat_id") == 2
    )
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='video_task.confirm-payment'"
            ).fetchone()[0]
            == 1
        )


def test_parent_can_request_free_reschedule_of_paid_consultation_from_telegram(app):
    _, owner, _, booking, _, _, _, _ = confirmed_consultation(app)
    q = quote(owner, booking)
    assert q.status_code == 201, q.json
    order = owner.post("/api/orders", json={"quote_id": q.json["id"], "key": "reschedule-bot"})
    assert order.status_code == 200, order.json
    assert pay_consultation(app, owner, order.json).status_code == 200
    paid = owner.get("/api/bookings/" + booking["id"]).json
    assert paid["status"] == "confirmed" and paid["data"]["paid_at"] is not None
    telegram = Telegram(app)
    telegram.bot(2).card(booking["id"], "booking")
    telegram.click(telegram.button("Перенести встречу", 2), 2)
    requested = owner.get("/api/bookings/" + booking["id"]).json
    assert requested["status"] == "reschedule_requested"
    assert requested["data"]["paid_at"] == paid["data"]["paid_at"]
    assert requested["data"]["order_id"] == order.json["id"]
    with connect(app.config["DATABASE"]) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM objects WHERE kind='order' AND json_extract(data,'$.consultation_run_id') IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "legacy_pdf_only", [False, True], ids=["audio-with-extra-pdf", "legacy-pdf-only"]
)
def test_telegram_redownload_preserves_additional_and_historical_materials(
    app, tmp_path, legacy_pdf_only
):
    admin, owner, task = submitted_task(app, tmp_path)
    pdf = result_pdf()
    uploaded = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/media",
        data={
            "reason": "Specialist supplemental material",
            "file": (io.BytesIO(pdf), "additional.pdf", "application/pdf"),
        },
    )
    assert uploaded.status_code == 201, uploaded.json
    assert owner.get("/api/media/" + uploaded.json["id"]).status_code == 404
    ids = [uploaded.json["id"]]
    if legacy_pdf_only:
        # A historical completed task, created before an audio response became mandatory.
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, task["id"])
            update(
                db,
                current,
                app.clock[0],
                {
                    **current["data"],
                    "answer_attachments": ids,
                    "recommendations": "Historical specialist result",
                    "answered_at": app.clock[0],
                },
                "completed",
            )
    else:
        audio = make_response_audio(tmp_path, "mp3")
        uploaded_audio = admin.post(
            "/api/admin/video-tasks/" + task["id"] + "/media",
            data={
                "reason": "Specialist audio response",
                "file": (io.BytesIO(audio), "answer.mp3", "audio/mpeg"),
            },
        )
        assert uploaded_audio.status_code == 201, uploaded_audio.json
        ids.insert(0, uploaded_audio.json["id"])
        published = admin.post(
            "/api/admin/video-tasks/" + task["id"] + "/recommendations",
            json={
                "revision": task["revision"],
                "text": "Audio answer with a personal supplement",
                "reason": "Approved by specialist",
                "attachments": ids,
            },
        )
        assert published.status_code == 200, published.json
    telegram = Telegram(app)
    telegram.bot(2).action("video_task_answer_download", {"id": task["id"]})
    assert telegram.files[-1][2] == pdf
    assert telegram.files[-1][4] == "sendDocument"
    assert len(telegram.files) == (1 if legacy_pdf_only else 2)
    if not legacy_pdf_only:
        assert telegram.files[0][4] == "sendAudio"
    for media_id in ids:
        assert telegram.bot(3).client.get("/api/media/" + media_id).status_code == 404
