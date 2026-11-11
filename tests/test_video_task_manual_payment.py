"""Manual specialist confirmations remain separate from provider payment records."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from marina.db import connect
from tests.test_consultation_commerce import app as consultation_app
from tests.test_consultation_remainder import task_setup, upload_task_response_audio
from tests.test_identity import login
from tests.test_video_task_audio_contract import publish_answer, submitted_task

app = consultation_app


def confirmation(task, **changes):
    return {
        "revision": task["revision"],
        "amount_rub": task["data"]["amount_rub"],
        "reference": "PRIVATE-reference-9001",
        "reason": "PRIVATE-manual-audit-only",
        **changes,
    }


def confirm(client, task, payload=None):
    return client.post(
        "/api/admin/video-tasks/" + task["id"] + "/confirm-payment",
        json=confirmation(task) if payload is None else payload,
    )


def client_task_views(owner, task):
    return [
        owner.get("/api/video-tasks/" + task["id"]).json,
        next(item for item in owner.get("/api/history").json["items"] if item["id"] == task["id"]),
        next(
            item
            for item in owner.get("/api/data-export").json["records"]
            if item["id"] == task["id"]
        ),
    ]


def assert_no_provider_payment(app):
    with connect(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM objects WHERE kind='order'").fetchone()[0] == 0


def assert_one_confirmation(app, task):
    with connect(app.config["DATABASE"]) as db:
        entries = db.execute(
            "SELECT * FROM audit WHERE object_id=? AND action='video_task.confirm-payment'",
            (task["id"],),
        ).fetchall()
        assert len(entries) == 1
    assert_no_provider_payment(app)


def test_unknown_manual_payment_is_explicit_in_client_views(app):
    _, owner, task = task_setup(app, paid=False)
    for item in client_task_views(owner, task):
        assert item["data"]["manual_payment"] == {
            "status": "unknown",
            "amount_rub": task["data"]["amount_rub"],
            "confirmed_at": None,
        }
    assert_no_provider_payment(app)


def test_specialist_confirms_exact_snapshot_amount_without_provider_receipt(app):
    admin, owner, task = task_setup(app, paid=False)
    actor = admin.get("/api/me").json["account"]["id"]
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail("Manual recording must not send HTTP")
    payload = confirmation(task, confirmed_by="client-spoof", confirmed_at=1, status="paid")
    response = confirm(admin, task, payload)
    assert response.status_code == 200, response.json
    saved = response.json
    assert task["status"] == "awaiting_payment"
    assert saved["status"] == "requested"
    assert saved["revision"] == task["revision"] + 1
    payment = saved["data"]["manual_payment"]
    assert payment == {
        "status": "confirmed",
        "amount_rub": task["data"]["amount_rub"],
        "confirmed_at": app.clock[0],
        "confirmed_by": actor,
        "reference": payload["reference"],
        "reason": payload["reason"],
        "request_revision": task["revision"],
    }
    assert saved["data"]["paid_at"] == app.clock[0]
    assert saved["data"]["order_id"] is None
    for item in client_task_views(owner, task):
        assert item["data"]["manual_payment"] == {
            "status": "confirmed",
            "amount_rub": task["data"]["amount_rub"],
            "confirmed_at": app.clock[0],
        }
        serialized = json.dumps(item)
        assert payload["reference"] not in serialized
        assert payload["reason"] not in serialized
    admin_object = admin.get("/api/admin/objects/" + task["id"]).json["object"]
    assert admin_object["data"]["manual_payment"] == payment
    with connect(app.config["DATABASE"]) as db:
        entry = db.execute(
            "SELECT * FROM audit WHERE object_id=? AND action='video_task.confirm-payment'",
            (task["id"],),
        ).fetchone()
        assert entry["actor"] == actor
        assert entry["reason"] == payload["reason"]
        assert entry["before_revision"] == task["revision"]
        assert entry["after_revision"] == saved["revision"]
        assert entry["at"] == app.clock[0]
        versions = db.execute(
            "SELECT revision,data FROM object_versions WHERE object_id=? ORDER BY revision",
            (task["id"],),
        ).fetchall()
        assert not json.loads(versions[0]["data"]).get("manual_payment")
        assert json.loads(versions[-1]["data"])["manual_payment"] == payment
    assert_one_confirmation(app, task)


def test_client_cannot_confirm_manual_payment(app):
    _, owner, task = task_setup(app, paid=False)
    assert confirm(owner, task).status_code == 403
    assert_no_provider_payment(app)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_rub": 0},
        {"amount_rub": -1},
        {"amount_rub": True},
        {"amount_rub": 1900.0},
        {"amount_rub": "1900"},
        {"reference": "   "},
        {"reference": None},
        {"reference": "x" * 241},
        {"reason": ""},
        {"reason": "no"},
    ],
)
def test_invalid_manual_confirmation_does_not_change_task(app, changes):
    admin, owner, task = task_setup(app, paid=False)
    rejected = confirm(admin, task, confirmation(task, **changes))
    assert rejected.status_code == 422, rejected.json
    current = owner.get("/api/video-tasks/" + task["id"]).json
    assert current["revision"] == task["revision"]
    assert current["data"]["manual_payment"]["status"] == "unknown"
    assert_no_provider_payment(app)


def test_manual_confirmation_rejects_amount_different_from_task_snapshot(app):
    admin, owner, task = task_setup(app, paid=False)
    response = confirm(admin, task, confirmation(task, amount_rub=task["data"]["amount_rub"] + 1))
    assert response.status_code == 422, response.json
    assert owner.get("/api/video-tasks/" + task["id"]).json["revision"] == task["revision"]
    assert_no_provider_payment(app)


def test_assignment_requires_payment_and_confirmation_rejects_stale_revision(app):
    admin, owner, task = task_setup(app, paid=False)
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={"revision": task["revision"], "reason": "Назначение", "text": "Задание"},
    )
    assert assigned.status_code == 409, assigned.json
    rejected = confirm(admin, task, confirmation(task, revision=task["revision"] - 1))
    assert rejected.status_code == 409, rejected.json
    assert owner.get("/api/video-tasks/" + task["id"]).json["revision"] == task["revision"]


def test_identical_manual_confirmation_retries_preserve_history_and_later_task_work(app):
    admin, owner, task = task_setup(app, paid=False)
    payload = confirmation(task)
    first = confirm(admin, task, payload)
    assert first.status_code == 200, first.json
    retry = confirm(admin, task, payload)
    assert retry.status_code == 200, retry.json
    assert retry.json == first.json
    fresh_retry = confirm(admin, task, {**payload, "revision": first.json["revision"]})
    assert fresh_retry.status_code == 200, fresh_retry.json
    assert fresh_retry.json == first.json
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={"revision": first.json["revision"], "reason": "Назначение", "text": "Задание"},
    )
    assert assigned.status_code == 200, assigned.json
    after_work = confirm(admin, task, payload)
    assert after_work.status_code == 200, after_work.json
    assert after_work.json["revision"] == assigned.json["revision"]
    assert after_work.json["status"] == "awaiting_videos"
    assert after_work.json["data"]["manual_payment"] == first.json["data"]["manual_payment"]
    stale = confirm(admin, task, {**payload, "revision": 0})
    assert stale.status_code == 409, stale.json
    assert_one_confirmation(app, task)


@pytest.mark.parametrize("changed", ["reference", "reason", "amount_rub", "actor"])
def test_manual_confirmation_cannot_be_overwritten(app, changed):
    admin, _, task = task_setup(app, paid=False)
    first = confirm(admin, task)
    assert first.status_code == 200, first.json
    payload = confirmation(task, revision=first.json["revision"])
    if changed == "actor":
        app.config["ADMIN_TELEGRAM_IDS"] = {1, 4}
        admin = login(app, 4)
    elif changed == "amount_rub":
        payload[changed] += 1
    else:
        payload[changed] += " different"
    rejected = confirm(admin, task, payload)
    assert rejected.status_code == 409, rejected.json
    persisted = admin.get("/api/admin/objects/" + task["id"]).json["object"]
    assert persisted == first.json
    assert_one_confirmation(app, task)


@pytest.mark.parametrize("same_reference", [True, False], ids=["duplicate", "conflict"])
def test_concurrent_manual_confirmations_record_once(app, same_reference):
    admin, _, task = task_setup(app, paid=False)
    token = admin.environ_base["HTTP_AUTHORIZATION"]
    barrier = Barrier(2)

    def submit(reference):
        client = app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = token
        barrier.wait(timeout=5)
        return confirm(client, task, confirmation(task, reference=reference))

    refs = ["concurrent-a", "concurrent-a" if same_reference else "concurrent-b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, refs))
    assert sorted(item.status_code for item in outcomes) == (
        [200, 200] if same_reference else [200, 409]
    )
    assert_one_confirmation(app, task)


def test_manual_confirmation_allows_task_execution_without_provider_receipt(app, tmp_path):
    admin, owner, task, _ = submitted_task(app, tmp_path)
    audio = upload_task_response_audio(admin, task, tmp_path)
    completed = publish_answer(admin, task, [audio["id"]])
    assert completed.status_code == 200, completed.json
    assert completed.json["status"] == "completed"
    client = owner.get("/api/video-tasks/" + task["id"]).json
    assert client["data"]["manual_payment"]["status"] == "confirmed"
    assert_no_provider_payment(app)
