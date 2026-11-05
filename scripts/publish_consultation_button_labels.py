"""Publish author button labels through audited APIs without changing money or PDFs."""

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from marina import create_app
from marina.bot import trusted_client
from marina.db import connect
from marina.funnel import published
from marina.commerce import policies


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--env-file", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--write-source", action="store_true")
    p.add_argument(
        "--spec",
        default=str(
            Path(__file__).resolve().parents[1]
            / "docs/content/consultation-remainder-20261105.json"
        ),
    )
    args = p.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    load_dotenv(args.env_file, override=True)
    app = create_app({"DATABASE": args.database})
    release = "consultation-buttons-source-20261105"
    why = "Владелец потребовал привести все названия кнопок и переходы из отложенных сообщений к ДЛЯ БОТА ОСТАТОК.pdf. Финансовые условия и исходные PNG в PDF сохраняются."

    def fingerprints():
        with connect(args.database) as db:
            financial = {
                t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("receipts", "fulfillments", "intent_fulfillments")
            }
            records = [
                tuple(r)
                for r in db.execute(
                    "SELECT id,revision,status,data FROM objects WHERE kind IN ('order','right','booking','funnel_run','video_review','video_task') ORDER BY id"
                )
            ]
            blobs = [
                tuple(r) for r in db.execute("SELECT id,digest,length(body) FROM blobs ORDER BY id")
            ]
        return {
            "financial": financial,
            "records": hashlib.sha256(json.dumps(records).encode()).hexdigest(),
            "blobs": hashlib.sha256(json.dumps(blobs).encode()).hexdigest(),
        }

    before = fingerprints()
    with connect(args.database) as db:
        admin = next(
            dict(r)
            for r in db.execute("SELECT * FROM accounts WHERE role='admin' AND blocked=0")
            if r["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"]
        )
    c, _ = trusted_client(app, {"id": admin["telegram_id"], "first_name": admin["name"]})

    def req(method, path, data=None):
        r = c.open("/api" + path, method=method, json=data)
        assert r.status_code < 400, (path, r.status_code, r.json)
        return r.json

    def publish(obj, data):
        data = copy.deepcopy(data)
        data.pop("approval", None)
        data["configuration_release"] = release
        if all(obj["data"].get(k) == v for k, v in data.items()):
            return obj
        draft = req(
            "POST",
            "/admin/content/" + obj["id"] + "/copy",
            {"revision": obj["revision"], "reason": why},
        )
        data["previous_version"] = draft["data"]["previous_version"]
        obj = req(
            "PATCH",
            "/admin/content/" + draft["id"],
            {"revision": draft["revision"], "data": data, "reason": why},
        )
        for action in ("approve", "publish"):
            obj = req(
                "POST",
                "/admin/content/" + obj["id"] + "/" + action,
                {"revision": obj["revision"], "reason": why},
            )
        return obj

    groups = {}
    source_data = {}
    try:
        for group, author in spec["ages"].items():
            with connect(args.database) as db:
                obj = published(db, group)
            data = copy.deepcopy(obj["data"])
            previous = copy.deepcopy(data)
            data["consultation_journey"]["buttons"] = author["buttons"]
            data["consultation_journey"]["contact_prefill"] = author["contact_prefill"]
            for f in data["followups"]:
                f["button_label"] = author["buttons"][
                    "book_target" if f.get("button_action") == "book" else "info"
                ]
                f["support_label"] = author["buttons"]["support"]
            for field in (
                "consultation_regular_rub",
                "consultation_amount_rub",
                "consultation_product_id",
                "guide_product_id",
            ):
                assert data[field] == previous[field]
            for field in ("preparation_material_id", "payment_timing"):
                assert (
                    data["consultation_journey"][field] == previous["consultation_journey"][field]
                )
            obj = publish(obj, data)
            source_data[group] = obj["data"]
            groups[group] = {
                "funnel_id": obj["id"],
                "buttons": obj["data"]["consultation_journey"]["buttons"],
                "preparation_material_id": obj["data"]["consultation_journey"][
                    "preparation_material_id"
                ],
                "regular_rub": obj["data"]["consultation_regular_rub"],
                "promo_rub": obj["data"]["consultation_amount_rub"],
            }
        with connect(args.database) as db:
            routes = policies(db, "consultation_routes")
        data = copy.deepcopy(routes["data"])
        for key, r in spec["outside"]["routes"].items():
            data["routes"][key]["contact_label"] = r["contact_label"]
        routes = publish(routes, data)
        after = fingerprints()
        assert before == after, "Existing money, records or BLOB changed"
        if args.write_source:
            path = Path(app.config["FUNNEL_SOURCE_PATH"])
            source = json.loads(path.read_text())
            info = path.stat()
            for group, data in source_data.items():
                source[group]["consultation_journey"]["buttons"] = data["consultation_journey"][
                    "buttons"
                ]
                source[group]["consultation_journey"]["contact_prefill"] = data[
                    "consultation_journey"
                ]["contact_prefill"]
                source[group]["followups"] = data["followups"]
            temp = path.with_suffix(".buttons-next")
            temp.write_text(json.dumps(source, ensure_ascii=False, indent=2))
            os.chmod(temp, info.st_mode & 0o777)
            os.chown(temp, info.st_uid, info.st_gid)
            os.replace(temp, path)
        result = {
            "release": release,
            "groups": groups,
            "routes_policy_id": routes["id"],
            "before": before,
            "after": after,
            "source_updated": args.write_source,
        }
        Path(args.manifest).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        req("POST", "/logout", {})


if __name__ == "__main__":
    main()
