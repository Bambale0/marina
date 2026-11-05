"""The baby video review is completed only with its own specialist audio answer."""

import io
from email import policy as email_policy
from email.parser import BytesParser

import pytest

from marina import object_get
from marina.bot import send_file
from marina.db import connect, transaction, update
from tests.test_consultation_commerce import app as consultation_app
from tests.test_consultation_remainder import (
    make_response_audio,
    task_setup,
    upload_task_response_audio,
)
from tests.test_crm import make_video
from tests.test_funnel import drain
from tests.test_identity import login

app = consultation_app


def submit_existing(admin, owner, task, raw):
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Синтетическое задание специалиста",
            "reason": "Назначение в изолированном тесте",
        },
    )
    assert assigned.status_code == 200, assigned.json
    for slot in range(1, 4):
        uploaded = owner.post(
            "/api/video-tasks/" + task["id"] + "/video",
            data={"slot": str(slot), "file": (io.BytesIO(raw), "task.mp4", "video/mp4")},
        )
        assert uploaded.status_code == 200, uploaded.json
    submitted = owner.post("/api/video-tasks/" + task["id"] + "/submit", json={})
    assert submitted.status_code == 200, submitted.json
    return submitted.json


def submitted_task(app, tmp_path):
    admin, owner, task = task_setup(app)
    raw = make_video(tmp_path)
    return admin, owner, submit_existing(admin, owner, task, raw), raw


def publish_answer(admin, task, attachments=None):
    payload = {
        "revision": task["revision"],
        "text": "Синтетическое описание ответа специалиста",
        "reason": "Публикация проверенного аудиоответа",
    }
    if attachments is not None:
        payload["attachments"] = attachments
    return admin.post("/api/admin/video-tasks/" + task["id"] + "/recommendations", json=payload)


def assert_still_submitted(app, owner, task):
    current = owner.get("/api/video-tasks/" + task["id"]).json
    assert current["status"] == "submitted"
    assert current["revision"] == task["revision"]
    assert current["data"]["recommendations"] is None
    assert not current["data"].get("answer_attachments")
    with connect(app.config["DATABASE"]) as db:
        assert not db.execute(
            "SELECT 1 FROM outbox WHERE object_id=? AND event='video_task.recommendations'",
            (task["id"],),
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM audit WHERE object_id=? AND action='video_task.recommendations'",
            (task["id"],),
        ).fetchone()


@pytest.mark.parametrize("replacement", ["none", "pdf", "video", "unselected_audio"])
def test_video_review_cannot_complete_without_selected_audio(app, tmp_path, replacement):
    admin, owner, task, raw = submitted_task(app, tmp_path)
    ids = None
    if replacement == "unselected_audio":
        upload_task_response_audio(admin, task, tmp_path)
        ids = []
    elif replacement in ("pdf", "video"):
        data, filename, mime = (
            (b"%PDF-1.4 synthetic", "answer.pdf", "application/pdf")
            if replacement == "pdf"
            else (raw, "answer.mp4", "video/mp4")
        )
        uploaded = admin.post(
            "/api/admin/video-tasks/" + task["id"] + "/media",
            data={"reason": "Дополнительный материал", "file": (io.BytesIO(data), filename, mime)},
        )
        assert uploaded.status_code == 201, uploaded.json
        ids = [uploaded.json["id"]]
    answer = publish_answer(admin, task, ids)
    assert answer.status_code == 422, answer.json
    assert_still_submitted(app, owner, task)


@pytest.mark.parametrize(
    "raw,filename,mime",
    [
        (b"OggS" + b"\0" * 20, "broken.ogg", "audio/ogg"),
        (b"ID3" + b"\0" * 20, "broken.mp3", "audio/mpeg"),
    ],
)
def test_audio_signature_alone_cannot_complete_video_review(app, tmp_path, raw, filename, mime):
    admin, owner, task, _ = submitted_task(app, tmp_path)
    uploaded = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/media",
        data={"reason": "Проверка повреждённого аудио", "file": (io.BytesIO(raw), filename, mime)},
    )
    if uploaded.status_code == 201:
        answer = publish_answer(admin, task, [uploaded.json["id"]])
        assert answer.status_code == 422, answer.json
    else:
        assert uploaded.status_code == 422, uploaded.json
    assert_still_submitted(app, owner, task)


@pytest.mark.parametrize("different_owner", [False, True], ids=["other-task", "other-owner"])
def test_video_review_rejects_audio_from_another_task(app, tmp_path, different_owner):
    admin, owner, task, raw = submitted_task(app, tmp_path)
    other = login(app, 3) if different_owner else owner
    if different_owner:
        privacy = other.get("/api/privacy").json
        consent = other.post(
            "/api/consents", json={"policy_id": privacy["policy"]["id"], "accepted": True}
        )
        assert consent.status_code == 200, consent.json
    lead = other.post(
        "/api/consultation-leads",
        json={"route": "baby_0_2", "action": "booking", "key": "other-task"},
    )
    assert lead.status_code == 201, lead.json
    created = other.post("/api/video-tasks", json={"lead_id": lead.json["id"]})
    assert created.status_code == 201, created.json
    other_task = submit_existing(admin, other, created.json, raw)
    audio = upload_task_response_audio(admin, other_task, tmp_path)
    answer = publish_answer(admin, task, [audio["id"]])
    assert answer.status_code == (404 if different_owner else 422), answer.json
    assert_still_submitted(app, owner, task)


@pytest.mark.parametrize("unavailable", ["deleted_blob", "not_ready"])
def test_video_review_rejects_unavailable_audio(app, tmp_path, unavailable):
    admin, owner, task, _ = submitted_task(app, tmp_path)
    audio = upload_task_response_audio(admin, task, tmp_path)
    with transaction(app.config["DATABASE"]) as db:
        if unavailable == "deleted_blob":
            db.execute("DELETE FROM blobs WHERE id=?", (audio["id"],))
        else:
            update(db, object_get(db, audio["id"]), app.clock[0], status="archived")
    answer = publish_answer(admin, task, [audio["id"]])
    assert answer.status_code == 422, answer.json
    assert_still_submitted(app, owner, task)


@pytest.mark.parametrize(
    "extension,mime,method", [("ogg", "audio/ogg", "sendVoice"), ("mp3", "audio/mpeg", "sendAudio")]
)
def test_valid_task_audio_is_private_until_publication_and_delivered_once(
    app, tmp_path, extension, mime, method
):
    admin, owner, task, _ = submitted_task(app, tmp_path)
    audio = upload_task_response_audio(admin, task, tmp_path, extension)
    path = "/api/media/" + audio["id"]
    assert owner.get(path).status_code == 404
    stranger = login(app, 3)
    assert stranger.get(path).status_code == 404
    original = admin.get(path)
    assert original.status_code == 200
    published = publish_answer(admin, task, [audio["id"]])
    assert published.status_code == 200, published.json
    assert published.json["status"] == "completed"
    assert published.json["data"]["answer_attachments"] == [audio["id"]]
    received = owner.get(path)
    assert received.status_code == 200
    assert received.mimetype == mime
    assert received.data == original.data
    assert stranger.get(path).status_code == 404
    files = []
    app.config["FILE_TRANSPORT"] = lambda *args: files.append(args)
    drain(app)
    drain(app)
    answers = [args for args in files if args[0] == 2 and args[1] == "recommendations." + extension]
    assert len(answers) == 1
    assert answers[0][2:] == (original.data, mime, method)


@pytest.mark.parametrize(
    "extension,mime,method,field",
    [
        ("ogg", "audio/ogg", "sendVoice", "voice"),
        ("mp3", "audio/mpeg", "sendAudio", "audio"),
        ("mp4", "video/mp4", "sendVideo", "video"),
        ("pdf", "application/pdf", "sendDocument", "document"),
    ],
)
def test_telegram_multipart_uses_the_method_file_parameter(
    app, tmp_path, monkeypatch, extension, mime, method, field
):
    if extension in ("ogg", "mp3"):
        raw = make_response_audio(tmp_path, extension)
    elif extension == "mp4":
        raw = make_video(tmp_path)
    else:
        raw = b"%PDF-1.4 synthetic transport fixture"
    app.config.pop("FILE_TRANSPORT", None)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')

    monkeypatch.setattr("marina.bot.urlopen", fake_urlopen)
    send_file(app, 2, "response." + extension, raw, mime, method)
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url.endswith("/" + method)
    envelope = BytesParser(policy=email_policy.default).parsebytes(
        b"Content-Type: " + request.get_header("Content-type").encode() + b"\r\n\r\n" + request.data
    )
    parts = {
        part.get_param("name", header="Content-Disposition"): part for part in envelope.iter_parts()
    }
    assert set(parts) == {"chat_id", field}
    assert parts["chat_id"].get_payload(decode=True) == b"2"
    assert parts[field].get_filename() == "response." + extension
    assert parts[field].get_content_type() == mime
    assert parts[field].get_payload(decode=True) == raw
