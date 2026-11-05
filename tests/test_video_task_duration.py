"""The owner allows valid task videos of any duration within the byte limit."""

import copy
import hashlib
import io
import json
import re
import subprocess

import pytest

from marina import object_get
from marina.bot import Bot
from marina.db import transaction, update
from marina.media import verify_video
from tests.test_consultation_commerce import app as consultation_app
from tests.test_consultation_remainder import route_setup, task_setup
from tests.test_funnel import drain
from tests.test_identity import login

app = consultation_app


def assigned_task(app, legacy):
    admin, owner, task = task_setup(app)
    if legacy:
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, task["id"])
            # Model a task already issued under the old policy, without migrating its history.
            data = {
                **current["data"],
                "rules": {**current["data"]["rules"], "max_video_seconds": 60},
            }
            task = update(db, current, app.clock[0], data)
    assigned = admin.post(
        "/api/admin/video-tasks/" + task["id"] + "/assignment",
        json={
            "revision": task["revision"],
            "text": "Выполните задание специалиста и запишите видео",
            "reason": "Синтетическое назначение для проверки загрузки",
        },
    )
    assert assigned.status_code == 200, assigned.json
    return owner, assigned.json


@pytest.mark.parametrize("duration", [61, 601])
@pytest.mark.parametrize("legacy", [False, True], ids=["new-task", "legacy-60-second-task"])
def test_video_task_accepts_video_longer_than_sixty_seconds(app, tmp_path, duration, legacy):
    owner, task = assigned_task(app, legacy)
    path = tmp_path / "long-video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s=32x32:r=1:d={duration}",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        timeout=15,
    )
    raw = path.read_bytes()
    assert 0 < len(raw) < 1000000
    assert float(verify_video(raw)["format"]["duration"]) == duration

    uploaded = owner.post(
        "/api/video-tasks/" + task["id"] + "/video",
        data={"slot": "1", "file": (io.BytesIO(raw), "long-video.mp4", "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.json
    assert len(uploaded.json["data"]["videos"]) == 1
    video_url = "/api/video-tasks/" + task["id"] + "/video/1"
    stored = owner.get(video_url)
    assert stored.status_code == 200
    assert stored.data == raw
    assert login(app, 3).get(video_url).status_code == 404


@pytest.mark.parametrize("legacy", [False, True], ids=["new-task", "legacy-60-second-task"])
def test_video_task_bot_prompt_and_assignment_do_not_limit_duration(app, legacy):
    _, task = assigned_task(app, legacy)
    calls = []
    app.config["HTTP_TRANSPORT"] = lambda method, url, data, headers: calls.append(data) or {
        "ok": True,
        "result": {"message_id": len(calls)},
    }
    drain(app)
    bot = Bot(app, {"id": 2, "first_name": "Мама"}, 2, "video-duration-prompt")
    bot.action("video_task_upload", {"id": task["id"], "slot": 1})
    texts = [call.get("text", "") for call in calls if call.get("chat_id") == 2]
    assignments = [text for text in texts if "Задание специалиста" in text]
    prompts = [text for text in texts if "Пришлите видео выполнения пункта 1" in text]
    assert len(assignments) == len(prompts) == 1, texts
    assert "3 видео" in assignments[0]
    assert "MP4" in prompts[0]
    assert not re.search(r"\bдо\s+\d+\s+секунд", assignments[0] + "\n" + prompts[0])


def test_legacy_route_defaults_and_new_task_ignore_duration_without_rewriting_policy(app):
    admin, owner, policy = route_setup(app)
    with transaction(app.config["DATABASE"]) as db:
        current = object_get(db, policy["id"])
        legacy_data = copy.deepcopy(current["data"])
        legacy_data["routes"]["baby_0_2"]["max_video_seconds"] = 60
        legacy_policy = update(db, current, app.clock[0], legacy_data)

    defaults = admin.get("/api/admin/consultation-route-defaults")
    assert defaults.status_code == 200, defaults.json
    assert "max_video_seconds" not in defaults.json["routes"]["baby_0_2"]
    assert defaults.json["routes"]["baby_0_2"]["required_videos"] == 3
    persisted = admin.get("/api/admin/content/" + policy["id"]).json
    assert persisted["revision"] == legacy_policy["revision"]
    assert persisted["data"] == legacy_data

    lead = owner.post(
        "/api/consultation-leads",
        json={"route": "baby_0_2", "action": "booking", "key": "legacy-route"},
    )
    assert lead.status_code == 201, lead.json
    assert lead.json["data"]["route_snapshot"]["max_video_seconds"] == 60
    created = owner.post("/api/video-tasks", json={"lead_id": lead.json["id"]})
    assert created.status_code == 201, created.json
    assert created.json["data"]["rules"]["max_video_seconds"] == 60
    assert created.json["data"]["rules"]["required_videos"] == 3
    repeated = owner.post("/api/video-tasks", json={"lead_id": lead.json["id"]})
    assert repeated.status_code == 201, repeated.json
    assert repeated.json["id"] == created.json["id"]


def test_existing_legacy_video_task_can_be_replayed_without_fingerprint_conflict(app):
    _, owner, task = task_setup(app)
    lead_id = task["data"]["lead_id"]
    with transaction(app.config["DATABASE"]) as db:
        lead = object_get(db, lead_id)
        legacy_lead_data = copy.deepcopy(lead["data"])
        legacy_lead_data["route_snapshot"]["max_video_seconds"] = 60
        update(db, lead, app.clock[0], legacy_lead_data)
        current = object_get(db, task["id"])
        legacy_task_data = copy.deepcopy(current["data"])
        legacy_task_data["rules"]["max_video_seconds"] = 60
        legacy_task = update(db, current, app.clock[0], legacy_task_data)
        # This is the persisted fingerprint created by the pre-change task payload.
        fingerprint = hashlib.sha256(
            json.dumps(legacy_task_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        changed = db.execute(
            "UPDATE dedup SET fingerprint=? WHERE scope=? AND key=?",
            (fingerprint, "video-task:" + task["owner"], lead_id),
        )
        assert changed.rowcount == 1

    repeated = owner.post("/api/video-tasks", json={"lead_id": lead_id})
    assert repeated.status_code == 201, repeated.json
    assert repeated.json["id"] == task["id"]
    assert repeated.json["revision"] == legacy_task["revision"]
    assert repeated.json["data"] == legacy_task_data
    with transaction(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM objects WHERE kind='video_task'").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE object_id=? AND event='video_task.requested'",
                (task["id"],),
            ).fetchone()[0]
            == 1
        )
