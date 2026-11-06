"""Publish the four author-supplied PDF delivery messages through audited admin APIs."""

import argparse
import copy
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from flask import g

from marina import Problem, create_app
from marina.bot import trusted_client
from marina.db import connect
from marina.funnel import AGES, published, validate_funnel
from scripts.publish_document_release import (
    ReleaseError,
    _guard_manifest_path,
    _request,
    _write_manifest,
    protected_fingerprint,
)

SPEC = Path(__file__).resolve().parents[1] / "docs/content/tripwire-delivery-20261106.json"
FIELDS = ("success_text", "guide_filename")
REASON = "Выдача трипваера по точным текстам ДЛЯ БОТА.docx, согласованным владельцем."


def load_spec():
    return json.loads(SPEC.read_text(encoding="utf-8"))


def _target(data, author):
    target = copy.deepcopy(data)
    for field in FIELDS:
        value = author[field]
        if not isinstance(value, str) or not value.strip():
            raise ReleaseError("Missing author delivery field: " + field)
        target[field] = value
    return target


def _write_source(path, original, target):
    info = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tripwire-", delete=False) as f:
            temporary = Path(f.name)
            f.write((json.dumps(target, ensure_ascii=False, indent=2) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, info.st_uid, info.st_gid)
        if path.read_bytes() != original:
            raise ReleaseError("Author source changed during publication")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_tripwire_delivery(app, spec=None, *, apply=False):
    spec = copy.deepcopy(spec if spec is not None else load_spec())
    database = app.config["DATABASE"]
    source_path = Path(app.config["FUNNEL_SOURCE_PATH"]).resolve(strict=True)
    original = source_path.read_bytes()
    source = json.loads(original)
    target_source = copy.deepcopy(source)
    changes = []
    before = protected_fingerprint(database)
    manifest = {"source_sha256": spec["source_sha256"], "changes": [], "publications": []}
    try:
        if set(spec["groups"]) != set(AGES):
            raise ReleaseError("All four author age groups are required")
        with connect(database) as db, app.test_request_context():
            db.execute("PRAGMA query_only=ON")
            g.db, g.app, g.now = db, app, int(app.config["CLOCK"]())
            admin = next(
                (
                    dict(r)
                    for r in db.execute("SELECT * FROM accounts WHERE role='admin' AND blocked=0")
                    if r["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"]
                ),
                None,
            )
            if admin is None:
                raise ReleaseError("An existing configured administrator is required")
            for group in AGES:
                current = published(db, group)
                if current is None:
                    raise ReleaseError("Published funnel is missing: " + group)
                target = _target(current["data"], spec["groups"][group])
                validate_funnel(target)
                target_source[group] = _target(source[group], spec["groups"][group])
                if target != current["data"]:
                    changes.append((group, current, target))
                    manifest["changes"].append(group)
    except (KeyError, TypeError, ValueError, Problem) as error:
        raise ReleaseError("Delivery copy preflight failed") from error
    if not apply:
        return {**manifest, "status": "planned", "source_changed": source != target_source}
    client = None
    try:
        if changes:
            client, _ = trusted_client(
                app, {"id": admin["telegram_id"], "first_name": admin["name"]}
            )
        for group, current, target in changes:
            with connect(database) as db:
                latest = published(db, group)
                if (latest["id"], latest["revision"]) != (current["id"], current["revision"]):
                    raise ReleaseError("Funnel changed after preflight: " + group)
            draft = _request(
                client,
                "POST",
                "/admin/content/" + current["id"] + "/copy",
                {"revision": current["revision"], "reason": REASON},
            )
            target.pop("approval", None)
            target["previous_version"] = draft["data"]["previous_version"]
            obj = _request(
                client,
                "PATCH",
                "/admin/content/" + draft["id"],
                {"revision": draft["revision"], "reason": REASON, "data": target},
            )
            for action in ("approve", "publish"):
                obj = _request(
                    client,
                    "POST",
                    "/admin/content/" + obj["id"] + "/" + action,
                    {"revision": obj["revision"], "reason": REASON},
                )
            if any(obj["data"][field] != target[field] for field in FIELDS):
                raise ReleaseError("Published delivery copy differs from author source")
            manifest["publications"].append({"group": group, "id": obj["id"]})
        if protected_fingerprint(database) != before:
            raise ReleaseError("Historical facts or BLOBs changed during copy publication")
        if source != target_source:
            _write_source(source_path, original, target_source)
        return {
            **manifest,
            "status": "applied" if changes or source != target_source else "unchanged",
        }
    finally:
        if client is not None:
            _request(client, "POST", "/logout", {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    database = Path(args.database).resolve(strict=True)
    source = os.environ.get("FUNNEL_SOURCE_PATH", "/var/lib/marina/content/screening_scenario.json")
    _guard_manifest_path(args.manifest, [database, source, SPEC, args.env_file])
    if args.once and args.apply and Path(args.manifest).exists():
        previous = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if (
            previous.get("status") in ("applied", "unchanged")
            and previous.get("source_sha256") == load_spec()["source_sha256"]
        ):
            print("Author delivery copy was already published; preserving subsequent admin edits")
            return
    with tempfile.TemporaryDirectory(prefix="marina-copy-") as directory:
        app = create_app({"DATABASE": str(Path(directory) / "bootstrap.sqlite")})
        app.config["DATABASE"] = str(database)
        result = publish_tripwire_delivery(app, apply=args.apply)
    _write_manifest(Path(args.manifest), result)
    print(json.dumps({"status": result["status"], "groups": result["changes"]}))


if __name__ == "__main__":
    main()
