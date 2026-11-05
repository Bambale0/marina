"""A narrow audited release preserves deployed configuration and historical facts."""

import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from marina.commerce import policies
from marina.db import connect, insert, transaction
from marina.funnel import published
from scripts import publish_document_release as release
from tests.test_commerce import product
from tests.test_consultation_commerce import CASES, app as consultation_app, login
from tests.test_funnel import approve_existing, scenario
from tests.test_screening import publish

app = consultation_app


def stored_rows(app):
    with connect(app.config["DATABASE"]) as db:
        return tuple(db.iterdump())


def source_without_release_fields(data):
    result = copy.deepcopy(data)
    for key in ("approval", "previous_version"):
        result.pop(key, None)
    for field in release.FIELDS:
        result["consultation_journey"].pop(field, None)
    return result


def material(admin):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(40, 750, "Synthetic existing PDF; do not replace during release")
    pdf.save()
    draft = admin.post(
        "/api/admin/content",
        json={
            "kind": "material",
            "title": "Existing synthetic approved PDF",
            "data": {"owner": "fixture", "status": "ready", "filename": "existing.pdf"},
            "reason": "Publisher integration fixture",
        },
    ).json
    asset = admin.post(
        "/api/admin/content/" + draft["id"] + "/asset",
        data={
            "reason": "Existing PDF attachment",
            "file": (io.BytesIO(stream.getvalue()), "existing.pdf", "application/pdf"),
        },
    )
    assert asset.status_code == 201, asset.json
    updated = admin.patch(
        "/api/admin/content/" + draft["id"],
        json={
            "revision": draft["revision"],
            "data": {**draft["data"], "asset_id": asset.json["id"]},
            "reason": "Bind existing PDF",
        },
    )
    assert updated.status_code == 200, updated.json
    return approve_existing(admin, updated.json)


@pytest.fixture
def deployed(app):
    admin, owner = login(app), login(app, 2)
    spec = json.loads(release.SPEC.read_text())
    existing_pdf = material(admin)
    funnels = {}
    for index, (group, base, promo) in enumerate(CASES):
        guide = product(
            admin,
            "guide",
            amount=49000,
            currency="RUB",
            age_group=group,
            material_id=existing_pdf["id"],
            lava_offer_id="KEEP-guide-" + group,
        )
        booking_product = product(
            admin, "booking", amount=base, currency="RUB", lava_offer_id="KEEP-booking-" + group
        )
        journey = copy.deepcopy(spec["ages"][group])
        for field in release.FIELDS:
            journey.pop(field)
        journey.update(
            payment_timing="before_scheduling" if index % 2 == 0 else "after_scheduling",
            preparation_material_id=existing_pdf["id"],
            retained_setting={"keep": [group, "nested"]},
        )
        journey["description"] += "\nExisting deployment wording must remain."
        funnels[group] = scenario(
            admin,
            group,
            guide_product_id=guide["id"],
            consultation_product_id=booking_product["id"],
            consultation_regular_rub=base // 100,
            consultation_amount_rub=promo,
            consultation_journey=journey,
            contact_url="https://t.me/synthetic_fixture",
            free_material_id=existing_pdf["id"],
            retained_setting={"owner_setting": index},
        )
    routes_data = copy.deepcopy(spec["outside"])
    routes_data["routes"]["baby_0_2"].update(
        material_id=existing_pdf["id"], max_video_seconds=60, retained_setting="keep-outside"
    )
    routes = publish(admin, "policy", routes_data)
    source_path = Path(app.config["FUNNEL_SOURCE_PATH"])
    source = {group: copy.deepcopy(funnel["data"]) for group, funnel in funnels.items()}
    source["deployment_metadata"] = {"keep": "private source extension"}
    for group in funnels:
        source[group]["consultation_journey"]["description"] += "\nSource-file-only wording."
    source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2))
    source_path.chmod(0o640)
    owner_id = owner.get("/api/me").json["account"]["id"]
    with transaction(app.config["DATABASE"]) as db:
        history = {}
        for kind in ("order", "booking", "funnel_run", "video_task"):
            history[kind] = insert(
                db,
                kind,
                {"sentinel": kind, "snapshot": "KEEP"},
                app.clock[0],
                owner_id,
                status="completed",
            )
        db.execute(
            "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)",
            ("lava", "synthetic-charge", history["order"]["id"], 190000, "RUB", 1, 2, "confirmed"),
        )
        db.execute(
            "INSERT INTO fulfillments VALUES(?,?,?)",
            (history["order"]["id"], "line", history["booking"]["id"]),
        )
        db.execute(
            "INSERT INTO intent_fulfillments VALUES(?,?,?,?)",
            ("intent", owner_id, history["order"]["id"], history["booking"]["id"]),
        )
        raw = b"historical private media bytes"
        db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                "history-blob",
                owner_id,
                history["video_task"]["id"],
                history["video_task"]["id"],
                "application/octet-stream",
                hashlib.sha256(raw).hexdigest(),
                raw,
                app.clock[0],
            ),
        )
    app.config["HTTP_TRANSPORT"] = lambda *args: pytest.fail(
        "Publisher must not call external HTTP"
    )
    app.config["FILE_TRANSPORT"] = lambda *args: pytest.fail("Publisher must not deliver files")
    return {
        "spec": spec,
        "funnels": funnels,
        "routes": routes,
        "source": source,
        "path": source_path,
    }


def test_plan_has_no_database_or_source_mutations(app, deployed, tmp_path):
    before_db = stored_rows(app)
    before_file = deployed["path"].read_bytes()
    manifest = tmp_path / "plan.json"
    result = release.publish_document_release(
        app, deployed["spec"], write_source=True, manifest_path=manifest
    )
    assert result["status"] == "planned"
    assert result["groups_checked"] == list(release.AGES)
    assert len(result["changes"]) == 5
    assert all(change["changed"] for change in result["changes"])
    assert not result["publications"] and not result["source_updated"]
    assert result["before"] == result["after"]
    assert stored_rows(app) == before_db
    assert deployed["path"].read_bytes() == before_file
    assert json.loads(manifest.read_text()) == result


def test_apply_preserves_every_unmanaged_field_and_history_then_is_idempotent(
    app, deployed, tmp_path
):
    before = release.protected_fingerprint(app.config["DATABASE"])
    source_stat = deployed["path"].stat()
    result = release.publish_document_release(
        app, deployed["spec"], apply=True, write_source=True, manifest_path=tmp_path / "apply.json"
    )
    assert result["status"] == "applied"
    assert len(result["publications"]) == 5
    assert result["source_updated"] is True
    assert result["before"] == result["after"] == before
    with connect(app.config["DATABASE"]) as db:
        for group, previous in deployed["funnels"].items():
            current = published(db, group)
            assert current["id"] != previous["id"]
            assert source_without_release_fields(current["data"]) == source_without_release_fields(
                previous["data"]
            )
            for field in release.FIELDS:
                assert (
                    current["data"]["consultation_journey"][field]
                    == deployed["spec"]["ages"][group][field]
                )
        routes = policies(db, "consultation_routes")
        expected_routes = release._stable_data(deployed["routes"]["data"])
        expected_routes["routes"]["baby_0_2"].pop("max_video_seconds")
        assert release._stable_data(routes["data"]) == expected_routes
        assert (
            db.execute("SELECT COUNT(*) FROM audit WHERE action='content.copy'").fetchone()[0] == 5
        )
    changed_source = json.loads(deployed["path"].read_text())
    assert changed_source["deployment_metadata"] == deployed["source"]["deployment_metadata"]
    for group in release.AGES:
        assert source_without_release_fields(
            changed_source[group]
        ) == source_without_release_fields(deployed["source"][group])
        for field in release.FIELDS:
            assert (
                changed_source[group]["consultation_journey"][field]
                == deployed["spec"]["ages"][group][field]
            )
    after_stat = deployed["path"].stat()
    assert (stat.S_IMODE(after_stat.st_mode), after_stat.st_uid, after_stat.st_gid) == (
        stat.S_IMODE(source_stat.st_mode),
        source_stat.st_uid,
        source_stat.st_gid,
    )
    before_repeat_db, before_repeat_file = stored_rows(app), deployed["path"].read_bytes()
    repeat = release.publish_document_release(app, deployed["spec"], apply=True, write_source=True)
    assert repeat["status"] == "unchanged"
    assert not repeat["publications"] and not repeat["source_updated"]
    assert stored_rows(app) == before_repeat_db
    assert deployed["path"].read_bytes() == before_repeat_file
    assert deployed["path"].stat().st_mtime_ns == after_stat.st_mtime_ns


def test_invalid_last_group_blocks_all_publication_and_source_changes(app, deployed, tmp_path):
    spec = copy.deepcopy(deployed["spec"])
    spec["ages"]["10-14"]["result_delivery"]["format"] = "video"
    before_db, before_file = stored_rows(app), deployed["path"].read_bytes()
    manifest = tmp_path / "blocked.json"
    with pytest.raises(release.ReleaseError):
        release.publish_document_release(
            app, spec, apply=True, write_source=True, manifest_path=manifest
        )
    report = json.loads(manifest.read_text())
    assert report["status"] == "blocked"
    assert report["groups_checked"] == list(release.AGES)
    assert any(error["scope"] == "10-14" for error in report["preflight_errors"])
    assert not report["publications"]
    assert stored_rows(app) == before_db
    assert deployed["path"].read_bytes() == before_file


def test_missing_motor_pdf_is_explicit_and_is_never_created(app, deployed):
    with transaction(app.config["DATABASE"]) as db:
        row = db.execute(
            "SELECT data FROM objects WHERE id=?", (deployed["routes"]["id"],)
        ).fetchone()
        data = json.loads(row["data"])
        data["routes"]["baby_0_2"]["material_id"] = ""
        db.execute(
            "UPDATE objects SET data=? WHERE id=?", (json.dumps(data), deployed["routes"]["id"])
        )
    before = release.protected_fingerprint(app.config["DATABASE"])
    report = release.publish_document_release(app, deployed["spec"], apply=True)
    assert report["status"] == "applied"
    assert report["commercial_ready"] is False
    assert report["readiness_blockers"][0]["code"] == "MOTOR_PDF_NOT_AVAILABLE"
    assert report["readiness_blockers"][0]["material_id"] is None
    assert release.protected_fingerprint(app.config["DATABASE"]) == before


def test_guard_detects_same_count_financial_edit_and_keeps_source_untouched(
    app, deployed, monkeypatch, tmp_path
):
    original = release._publish

    def tamper_after_publication(*args):
        result = original(*args)
        with transaction(app.config["DATABASE"]) as db:
            db.execute("UPDATE receipts SET amount=amount+1")
        return result

    monkeypatch.setattr(release, "_publish", tamper_after_publication)
    before_file = deployed["path"].read_bytes()
    manifest = tmp_path / "guard-failure.json"
    with pytest.raises(release.ReleaseError, match="Protected"):
        release.publish_document_release(
            app, deployed["spec"], apply=True, write_source=True, manifest_path=manifest
        )
    report = json.loads(manifest.read_text())
    assert report["status"] == "failed"
    old, new = report["before"]["financial"]["receipts"], report["after"]["financial"]["receipts"]
    assert old["rows"] == new["rows"] == 1
    assert old["sha256"] != new["sha256"]
    assert len(report["publications"]) == 1
    assert deployed["path"].read_bytes() == before_file


def test_blob_guard_hashes_bytes_even_when_stored_digest_and_length_are_unchanged(app, deployed):
    before = release.protected_fingerprint(app.config["DATABASE"])
    with transaction(app.config["DATABASE"]) as db:
        raw = db.execute("SELECT body FROM blobs WHERE id='history-blob'").fetchone()[0]
        db.execute("UPDATE blobs SET body=? WHERE id='history-blob'", (b"X" + raw[1:],))
    after = release.protected_fingerprint(app.config["DATABASE"])
    assert before["blobs"]["rows"] == after["blobs"]["rows"]
    assert before["blobs"]["sha256"] != after["blobs"]["sha256"]


def test_publisher_requires_existing_configured_admin(app, deployed):
    app.config["ADMIN_TELEGRAM_IDS"] = {999}
    before = stored_rows(app)
    with pytest.raises(release.ReleaseError):
        release.publish_document_release(app, deployed["spec"], apply=True)
    assert stored_rows(app) == before


def test_cli_plan_preserves_target_database_and_private_source(app, deployed, tmp_path):
    env = tmp_path / "publisher.env"
    env.write_text(
        "BOT_TOKEN=synthetic-publisher-token\nADMIN_TELEGRAM_IDS=1\nFUNNEL_SOURCE_PATH="
        + str(deployed["path"])
        + "\n"
    )
    manifest = tmp_path / "cli-plan.json"
    before = stored_rows(app)
    process = subprocess.run(
        [
            sys.executable,
            str(Path(release.__file__)),
            "--database",
            app.config["DATABASE"],
            "--env-file",
            str(env),
            "--manifest",
            str(manifest),
            "--plan",
            "--write-source",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(manifest.read_text())["status"] == "planned"
    assert stored_rows(app) == before
    assert json.loads(deployed["path"].read_text()) == deployed["source"]


@pytest.mark.parametrize("protected_name", ["database", "source", "env", "spec"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_cli_plan_rejects_manifest_aliases_before_touching_inputs(
    app, deployed, tmp_path, protected_name, alias
):
    env = tmp_path / "guard.env"
    env.write_text(
        "BOT_TOKEN=synthetic-publisher-token\nADMIN_TELEGRAM_IDS=1\nFUNNEL_SOURCE_PATH="
        + str(deployed["path"])
        + "\n"
    )
    spec = tmp_path / "guard-spec.json"
    spec.write_text(json.dumps(deployed["spec"]))
    inputs = {
        "database": Path(app.config["DATABASE"]),
        "source": deployed["path"],
        "env": env,
        "spec": spec,
    }
    protected = inputs[protected_name]
    manifest = protected
    if alias != "direct":
        manifest = tmp_path / "dangerous-manifest"
        if alias == "symlink":
            manifest.symlink_to(protected)
        else:
            os.link(protected, manifest)
    before = {name: path.read_bytes() for name, path in inputs.items()}
    process = subprocess.run(
        [
            sys.executable,
            str(Path(release.__file__)),
            "--database",
            str(inputs["database"]),
            "--env-file",
            str(env),
            "--spec",
            str(spec),
            "--manifest",
            str(manifest),
            "--plan",
            "--write-source",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 2
    assert "aliases a protected input" in process.stderr
    assert {name: path.read_bytes() for name, path in inputs.items()} == before


def test_null_result_rule_cannot_erase_an_existing_published_obligation(app, deployed):
    release.publish_document_release(app, deployed["spec"], apply=True)
    invalid = copy.deepcopy(deployed["spec"])
    invalid["ages"]["10-14"]["result_delivery"] = None
    before_db, before_source = stored_rows(app), deployed["path"].read_bytes()
    with pytest.raises(release.ReleaseError) as error:
        release.publish_document_release(app, invalid, apply=True, write_source=True)
    assert error.value.manifest["status"] == "blocked"
    assert error.value.manifest["groups_checked"] == list(release.AGES)
    assert stored_rows(app) == before_db
    assert deployed["path"].read_bytes() == before_source


def test_manifest_parent_is_checked_before_any_publication(app, deployed, tmp_path):
    before_db, before_source = stored_rows(app), deployed["path"].read_bytes()
    with pytest.raises(release.ReleaseError, match="Cannot prepare manifest"):
        release.publish_document_release(
            app,
            deployed["spec"],
            apply=True,
            write_source=True,
            manifest_path=tmp_path / "missing-directory" / "manifest.json",
        )
    assert stored_rows(app) == before_db
    assert deployed["path"].read_bytes() == before_source


def test_manifest_reports_replaced_source_after_directory_fsync_failure(
    app, deployed, tmp_path, monkeypatch
):
    original_fsync = release.os.fsync
    failed = False

    def fail_directory_once(descriptor):
        nonlocal failed
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed:
            failed = True
            raise OSError("Synthetic source directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(release.os, "fsync", fail_directory_once)
    manifest = tmp_path / "source-recovery.json"
    with pytest.raises(release.ReleaseError, match="fsync failure"):
        release.publish_document_release(
            app, deployed["spec"], apply=True, write_source=True, manifest_path=manifest
        )
    report = json.loads(manifest.read_text())
    assert report["status"] == "failed"
    assert report["source_updated"] is True
    assert report["source"]["state"] == "target"
    assert report["source"]["matches_target"] is True
    assert (
        report["source"]["after_sha256"]
        == hashlib.sha256(deployed["path"].read_bytes()).hexdigest()
    )
