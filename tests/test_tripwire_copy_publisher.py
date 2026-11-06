import copy
import json
import sys

import pytest

from marina.db import connect
from marina.funnel import published
from scripts import publish_tripwire_delivery as release
from tests.test_identity import app, login
from tests.test_funnel import private_source, scenario, start


def prepare(app):
    admin, owner = login(app), login(app, 2)
    for group in release.AGES:
        scenario(admin, group)
    run = start(owner)
    spec = copy.deepcopy(release.load_spec())
    return admin, run, spec


def test_plan_is_read_only_then_apply_is_audited_and_repeatable(app):
    admin, run, spec = prepare(app)
    before = release.protected_fingerprint(app.config["DATABASE"])
    with connect(app.config["DATABASE"]) as db:
        original = tuple(db.iterdump())
    plan = release.publish_tripwire_delivery(app, spec)
    assert plan["status"] == "planned"
    assert len(plan["changes"]) == 4
    with connect(app.config["DATABASE"]) as db:
        assert tuple(db.iterdump()) == original

    result = release.publish_tripwire_delivery(app, spec, apply=True)
    assert result["status"] == "applied"
    assert len(result["publications"]) == 4
    assert release.protected_fingerprint(app.config["DATABASE"]) == before
    with connect(app.config["DATABASE"]) as db:
        for group in release.AGES:
            current = published(db, group)
            assert current["data"]["success_text"] == spec["groups"][group]["success_text"]
            assert current["data"]["guide_filename"] == spec["groups"][group]["guide_filename"]
        historical = json.loads(
            db.execute("SELECT data FROM objects WHERE id=?", (run["id"],)).fetchone()[0]
        )
        assert historical["snapshot"]["success_text"] != spec["groups"]["3-4"]["success_text"]
    source = json.loads(release.Path(app.config["FUNNEL_SOURCE_PATH"]).read_text(encoding="utf-8"))
    assert all(source[g]["success_text"] == spec["groups"][g]["success_text"] for g in release.AGES)
    assert release.publish_tripwire_delivery(app, spec, apply=True)["status"] == "unchanged"
    audit = admin.get("/api/admin/audit").json["items"]
    assert any(item["reason"] == release.REASON for item in audit)


def test_invalid_group_blocks_all_publications(app):
    _, _, spec = prepare(app)
    spec["groups"]["10-14"]["success_text"] = ""
    with connect(app.config["DATABASE"]) as db:
        before = tuple(db.iterdump())
    with pytest.raises(release.ReleaseError):
        release.publish_tripwire_delivery(app, spec, apply=True)
    with connect(app.config["DATABASE"]) as db:
        assert tuple(db.iterdump()) == before


def test_once_marker_preserves_subsequent_admin_edits(app, tmp_path, monkeypatch):
    _, _, spec = prepare(app)
    manifest = tmp_path / "published.json"
    manifest.write_text(json.dumps({"status": "applied", "source_sha256": spec["source_sha256"]}))
    env_file = tmp_path / "release.env"
    env_file.write_text("")
    monkeypatch.setenv("FUNNEL_SOURCE_PATH", app.config["FUNNEL_SOURCE_PATH"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish",
            "--database",
            app.config["DATABASE"],
            "--env-file",
            str(env_file),
            "--manifest",
            str(manifest),
            "--apply",
            "--once",
        ],
    )
    with connect(app.config["DATABASE"]) as db:
        before = tuple(db.iterdump())
    release.main()
    with connect(app.config["DATABASE"]) as db:
        assert tuple(db.iterdump()) == before
