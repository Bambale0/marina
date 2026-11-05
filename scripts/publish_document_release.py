"""Plan or publish the narrow document release without rewriting existing obligations."""

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from flask import g

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marina import Problem, create_app  # noqa: E402
from marina.bot import trusted_client  # noqa: E402
from marina.commerce import policies  # noqa: E402
from marina.consultation_journey import material_ready  # noqa: E402
from marina.content import validate  # noqa: E402
from marina.db import connect  # noqa: E402
from marina.funnel import AGES, published  # noqa: E402

FIELDS = ("result_delivery", "contact_service", "session_minutes")
SPEC = Path(__file__).resolve().parents[1] / "docs/content/consultation-remainder-20261105.json"
REASON = (
    "Публикация согласованных правил персонального результата, ответа специалиста и "
    "длительности встречи из ДЛЯ БОТА ОСТАТОК.pdf. Остальные условия и материалы сохранены."
)


class ReleaseError(RuntimeError):
    def __init__(self, message, manifest=None):
        super().__init__(message)
        self.manifest = manifest


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _guard_manifest_path(manifest_path, inputs):
    destination = Path(manifest_path).resolve()
    for input_path in inputs:
        protected = Path(input_path).resolve()
        if destination == protected or (
            destination.exists() and protected.exists() and os.path.samefile(destination, protected)
        ):
            raise ReleaseError("Manifest path aliases a protected input: " + str(protected))
    return destination


def _manifest_destination(manifest_path, inputs):
    destination = _guard_manifest_path(manifest_path, inputs)
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".manifest-check-"):
            pass
        if destination.exists() and not destination.is_file():
            raise OSError("Manifest destination is not a regular file")
    except OSError as error:
        raise ReleaseError("Cannot prepare manifest destination: " + str(error)) from error
    return destination


def _write_manifest(path, manifest):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".manifest-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def row_fingerprint(rows):
    checksum, count = hashlib.sha256(), 0
    for row in rows:
        values = [
            {"bytes": len(value), "sha256": digest(value)} if isinstance(value, bytes) else value
            for value in row
        ]
        checksum.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
        checksum.update(b"\n")
        count += 1
    return {"rows": count, "sha256": checksum.hexdigest()}


def protected_fingerprint(database):
    """Hash row values and actual BLOB bytes, including versions of historical records."""
    protected = (
        "kind NOT IN ('funnel','policy') OR "
        "(kind='policy' AND COALESCE(json_extract(data,'$.type'),'')!='consultation_routes') OR "
        "(kind='funnel' AND json_extract(data,'$.age_group') NOT IN ('3-4','5-6','7-9','10-14'))"
    )
    with connect(database) as db:
        db.execute("BEGIN")
        financial = {
            table: row_fingerprint(db.execute("SELECT * FROM " + table + " ORDER BY rowid"))
            for table in ("receipts", "fulfillments", "intent_fulfillments")
        }
        records = row_fingerprint(
            db.execute("SELECT * FROM objects WHERE " + protected + " ORDER BY id")
        )
        versions = row_fingerprint(
            db.execute(
                "SELECT * FROM object_versions WHERE object_id IN (SELECT id FROM objects WHERE "
                + protected
                + ") ORDER BY object_id,revision"
            )
        )
        blobs = row_fingerprint(db.execute("SELECT * FROM blobs ORDER BY id"))
    return {"financial": financial, "records": records, "record_versions": versions, "blobs": blobs}


def _stable_data(data):
    result = copy.deepcopy(data)
    for key in ("approval", "previous_version"):
        result.pop(key, None)
    return result


def _updated_journey(data, author):
    target = copy.deepcopy(data)
    if not isinstance(target, dict) or not isinstance(target.get("consultation_journey"), dict):
        raise ReleaseError(
            "Existing consultation_journey is required; it will not be reconstructed"
        )
    if any(field not in author for field in FIELDS):
        raise ReleaseError("Author specification must contain all three release fields")
    if not isinstance(author["result_delivery"], dict):
        raise ReleaseError(
            "Author result_delivery must be an object; an existing obligation cannot be removed"
        )
    if not isinstance(author["contact_service"], dict):
        raise ReleaseError("Author contact_service must be an object")
    if type(author["session_minutes"]) is not int or author["session_minutes"] <= 0:
        raise ReleaseError("Author session_minutes must be a positive integer")
    for field in FIELDS:
        target["consultation_journey"][field] = copy.deepcopy(author[field])
    return target


def _source_plan(app, spec):
    path = Path(app.config["FUNNEL_SOURCE_PATH"]).resolve(strict=True)
    original = path.read_bytes()
    data = json.loads(original)
    target = copy.deepcopy(data)
    for group in AGES:
        target[group] = _updated_journey(target[group], spec["ages"][group])
    info = path.stat()
    return {
        "path": path,
        "original": original,
        "target": target,
        "changed": target != data,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _prepare_source(source):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=source["path"].parent, prefix=".document-release-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.fchown(stream.fileno(), source["uid"], source["gid"])
            os.fchmod(stream.fileno(), source["mode"])
            stream.write(
                (json.dumps(source["target"], ensure_ascii=False, indent=2) + "\n").encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _replace_source(source, temporary):
    info = source["path"].stat()
    if source["path"].read_bytes() != source["original"] or (
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
    ) != (source["mode"], source["uid"], source["gid"]):
        raise ReleaseError("FUNNEL_SOURCE_PATH changed after preflight; refusing to overwrite it")
    os.replace(temporary, source["path"])
    source["replace_completed"] = True
    descriptor = os.open(source["path"].parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _request(client, method, path, payload=None):
    response = client.open("/api" + path, method=method, json=payload)
    if response.status_code >= 400:
        message = (response.get_json(silent=True) or {}).get("error", "Request failed")
        raise ReleaseError(f"{method} {path}: {response.status_code} {message}")
    return response.json


def _publish(client, current, target):
    path = "/admin/content/" + current["id"]
    draft = _request(
        client, "POST", path + "/copy", {"revision": current["revision"], "reason": REASON}
    )
    payload = copy.deepcopy(target)
    payload.pop("approval", None)
    payload["previous_version"] = draft["data"]["previous_version"]
    updated = _request(
        client,
        "PATCH",
        "/admin/content/" + draft["id"],
        {"revision": draft["revision"], "data": payload, "reason": REASON},
    )
    for action in ("approve", "publish"):
        updated = _request(
            client,
            "POST",
            "/admin/content/" + updated["id"] + "/" + action,
            {"revision": updated["revision"], "reason": REASON},
        )
    if _stable_data(updated["data"]) != _stable_data(target):
        raise ReleaseError("Published payload differs from the reviewed plan")
    return updated


def publish_document_release(
    app, spec=None, *, apply=False, write_source=False, manifest_path=None
):
    """Preflight every age group, then publish copies using the configured existing admin."""
    database = app.config["DATABASE"]
    if manifest_path is not None:
        manifest_path = _manifest_destination(
            manifest_path, [database, app.config["FUNNEL_SOURCE_PATH"], SPEC]
        )
    spec = copy.deepcopy(spec if spec is not None else json.loads(SPEC.read_text()))
    manifest = {
        "release": "document-release-20261105",
        "mode": "apply" if apply else "plan",
        "status": "preflight",
        "journey_fields": list(FIELDS),
        "groups_checked": [],
        "changes": [],
        "publications": [],
        "preflight_errors": [],
        "readiness_blockers": [],
        "commercial_ready": False,
        "commercial_readiness_note": "Configuration publication does not establish commercial acceptance.",
        "source_updated": False,
        "before": protected_fingerprint(database),
    }
    changes, source, temporary, client = [], None, None, None
    try:
        with connect(database) as db, app.test_request_context():
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            g.db, g.app, g.now = db, app, int(app.config["CLOCK"]())
            admin = next(
                (
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM accounts WHERE role='admin' AND blocked=0 ORDER BY id"
                    )
                    if row["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"]
                ),
                None,
            )
            if admin is None:
                manifest["preflight_errors"].append(
                    {"scope": "admin", "error": "No configured active existing admin"}
                )
            for group in AGES:
                manifest["groups_checked"].append(group)
                try:
                    current = published(db, group)
                    if current is None:
                        raise ReleaseError("Published funnel is missing")
                    target = _updated_journey(current["data"], spec["ages"][group])
                    validate({**current, "data": target})
                    changed = target != current["data"]
                    manifest["changes"].append(
                        {
                            "scope": group,
                            "id": current["id"],
                            "revision": current["revision"],
                            "changed": changed,
                            "journey_values": {
                                key: target["consultation_journey"][key] for key in FIELDS
                            },
                        }
                    )
                    if changed:
                        changes.append((group, current, target))
                except (ReleaseError, Problem, KeyError, TypeError, ValueError) as error:
                    manifest["preflight_errors"].append(
                        {"scope": group, "error": getattr(error, "message", str(error))}
                    )
            try:
                routes = policies(db, "consultation_routes")
                if routes is None:
                    raise ReleaseError("Published consultation_routes policy is missing")
                target = copy.deepcopy(routes["data"])
                baby = target["routes"]["baby_0_2"]
                baby.pop("max_video_seconds", None)
                validate({**routes, "data": target})
                changed = target != routes["data"]
                manifest["changes"].append(
                    {
                        "scope": "outside",
                        "id": routes["id"],
                        "revision": routes["revision"],
                        "changed": changed,
                    }
                )
                if changed:
                    changes.append(("outside", routes, target))
                if not material_ready(db, baby.get("material_id")):
                    manifest["readiness_blockers"].append(
                        {
                            "code": "MOTOR_PDF_NOT_AVAILABLE",
                            "scope": "baby_0_2",
                            "material_id": baby.get("material_id") or None,
                            "filename": baby.get("material_filename", ""),
                            "message": "The approved motor-development PDF is not published and attached; no replacement is created.",
                        }
                    )
            except (ReleaseError, Problem, KeyError, TypeError, ValueError) as error:
                manifest["preflight_errors"].append(
                    {"scope": "outside", "error": getattr(error, "message", str(error))}
                )
        if write_source:
            try:
                source = _source_plan(app, spec)
                manifest["source"] = {
                    "path": str(source["path"]),
                    "changed": source["changed"],
                    "before_sha256": digest(source["original"]),
                }
            except (OSError, ReleaseError, KeyError, TypeError, ValueError) as error:
                manifest["preflight_errors"].append({"scope": "source", "error": str(error)})
        if manifest["preflight_errors"]:
            manifest["status"] = "blocked"
            if apply:
                raise ReleaseError("Release preflight failed; no publication was attempted")
            return manifest
        if not apply:
            manifest["status"] = "planned"
            return manifest
        if source and source["changed"]:
            temporary = _prepare_source(source)
        if changes:
            client, _ = trusted_client(
                app, {"id": admin["telegram_id"], "first_name": admin["name"]}
            )
        for scope, current, target in changes:
            result = _publish(client, current, target)
            manifest["publications"].append(
                {"scope": scope, "id": result["id"], "revision": result["revision"]}
            )
            if protected_fingerprint(database) != manifest["before"]:
                raise ReleaseError("Protected financial rows, historical records or BLOBs changed")
        if protected_fingerprint(database) != manifest["before"]:
            raise ReleaseError("Protected financial rows, historical records or BLOBs changed")
        if temporary is not None:
            _replace_source(source, temporary)
            temporary = None
            manifest["source_updated"] = True
            manifest["source"]["after_sha256"] = digest(source["path"].read_bytes())
        manifest["status"] = "applied" if changes or manifest["source_updated"] else "unchanged"
        return manifest
    except Exception as error:
        if manifest["status"] != "blocked":
            manifest["status"] = "failed"
        manifest["error"] = str(error)
        raise ReleaseError(str(error), manifest) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        cleanup_error = None
        if client is not None:
            try:
                _request(client, "POST", "/logout", {})
            except ReleaseError as error:
                cleanup_error = str(error)
        manifest["after"] = protected_fingerprint(database)
        if manifest["after"] != manifest["before"]:
            cleanup_error = "Protected financial rows, historical records or BLOBs changed"
        if source is not None:
            manifest["source_updated"] = bool(source.get("replace_completed"))
            try:
                actual = source["path"].read_bytes()
                matches = json.loads(actual) == source["target"]
                manifest["source"].update(
                    after_sha256=digest(actual),
                    matches_target=matches,
                    state="target"
                    if matches
                    else "original"
                    if actual == source["original"]
                    else "different",
                )
            except (OSError, ValueError) as error:
                manifest["source"].update(state="unknown", readback_error=str(error))
        if cleanup_error:
            manifest["status"], manifest["error"] = "failed", cleanup_error
        if manifest_path is not None:
            _write_manifest(manifest_path, manifest)
        if cleanup_error:
            raise ReleaseError(cleanup_error, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--manifest", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--plan", "--dry-run", action="store_true")
    parser.add_argument("--write-source", action="store_true")
    parser.add_argument("--spec", default=str(SPEC))
    args = parser.parse_args(argv)
    try:
        database = Path(args.database).resolve(strict=True)
        env_file = Path(args.env_file).resolve(strict=True)
        spec_path = Path(args.spec).resolve(strict=True)
        _guard_manifest_path(args.manifest, [database, env_file, spec_path])
        environment = dotenv_values(env_file)
        source_path = environment.get("FUNNEL_SOURCE_PATH")
        if source_path is None:
            source_path = os.environ.get(
                "FUNNEL_SOURCE_PATH", "/var/lib/marina/content/screening_scenario.json"
            )
        manifest_path = _manifest_destination(
            args.manifest, [database, env_file, spec_path, source_path]
        )
        load_dotenv(env_file, override=True)
        spec = json.loads(spec_path.read_text())
        # Bootstrap Flask on disposable storage; planning must not run schema/backfill on the target DB.
        with tempfile.TemporaryDirectory(prefix="marina-release-bootstrap-") as directory:
            app = create_app({"DATABASE": str(Path(directory) / "bootstrap.sqlite")})
            app.config["DATABASE"] = str(database)
            result = publish_document_release(
                app,
                spec,
                apply=args.apply,
                write_source=args.write_source,
                manifest_path=manifest_path,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["preflight_errors"] else 0
    except (OSError, ValueError, ReleaseError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
