"""Publish supplied preparation PDFs via audited admin APIs."""

import argparse
import copy
import hashlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from marina import create_app
from marina.bot import trusted_client
from marina.db import connect, unpack
from marina.funnel import published


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--env-file", required=True)
    p.add_argument("--files", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--write-source", action="store_true")
    args = p.parse_args()
    files = json.loads(Path(args.files).read_text())
    assert {f["group"] for f in files} == {"3-4", "5-6", "7-9", "10-14"}
    load_dotenv(args.env_file, override=True)
    app = create_app({"DATABASE": args.database, "DEV_OFFER_BYPASSES_CONSENT": False})
    why = "Владелец прислал четыре изображения памяток и поручил сделать четыре PDF и добавить в проект. Текст и структура перенесены с изображений; подключаем авторские памятки и оплату до согласования времени."
    release = "author-preparation-pdfs-20261105"

    def fingerprint():
        with connect(args.database) as db:
            counts = {
                t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("receipts", "fulfillments", "intent_fulfillments")
            }
            rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT id,revision,status,data FROM objects WHERE kind IN ('order','right','booking','funnel_run','video_review','video_task') ORDER BY id"
                )
            ]
            blobs = {
                r["id"]: (r["digest"], len(r["body"]), hashlib.sha256(r["body"]).hexdigest())
                for r in db.execute("SELECT id,digest,body FROM blobs")
            }
        return {
            "financial": counts,
            "records": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            "blobs": blobs,
        }

    before = fingerprint()
    with connect(args.database) as db:
        admin = next(
            dict(r)
            for r in db.execute("SELECT * FROM accounts WHERE role='admin' AND blocked=0")
            if r["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"]
        )
    client, _ = trusted_client(app, {"id": admin["telegram_id"], "first_name": admin["name"]})

    def req(method, path, data=None):
        r = client.open("/api" + path, method=method, json=data)
        assert r.status_code < 400, (method, path, r.status_code, r.json)
        return r.json

    def activate(obj):
        for action in ("approve", "publish"):
            obj = req(
                "POST",
                "/admin/content/" + obj["id"] + "/" + action,
                {"revision": obj["revision"], "reason": why},
            )
        return obj

    groups = {}
    try:
        for f in files:
            raw = Path(f["path"]).read_bytes()
            assert raw.startswith(b"%PDF-") and hashlib.sha256(raw).hexdigest() == f["sha256"]
            with connect(args.database) as db:
                material = next(
                    (
                        unpack(r)
                        for r in db.execute(
                            "SELECT * FROM objects WHERE kind='material' AND status='published'"
                        )
                        if json.loads(r["data"]).get("configuration_release") == release
                        and json.loads(r["data"]).get("age_group") == f["group"]
                        and json.loads(r["data"]).get("file_sha256") == f["sha256"]
                    ),
                    None,
                )
            if material is None:
                data = {
                    "title": "Памятка подготовки к консультации: " + f["group"] + " лет",
                    "owner": "Савельева Марина Валерьевна",
                    "status": "received",
                    "format": "pdf",
                    "age_group": f["group"],
                    "filename": f["filename"],
                    "source": "Изображение "
                    + f["source_image"]
                    + ", предоставленное владельцем проекта 05.11.2026. Текст перенесён и свёрстан в PDF.",
                    "rights": "Предоставлено владельцем проекта для размещения в проекте Марина.",
                    "file_sha256": f["sha256"],
                    "configuration_release": release,
                }
                material = req(
                    "POST",
                    "/admin/content",
                    {"kind": "material", "title": data["title"], "data": data, "reason": why},
                )
                response = client.post(
                    "/api/admin/content/" + material["id"] + "/asset",
                    data={
                        "reason": why,
                        "file": (io.BytesIO(raw), f["filename"], "application/pdf"),
                    },
                )
                assert response.status_code == 201, response.json
                data["asset_id"] = response.json["id"]
                material = req(
                    "PATCH",
                    "/admin/content/" + material["id"],
                    {"revision": material["revision"], "data": data, "reason": why},
                )
                material = activate(material)
            r = client.get("/api/admin/materials/" + material["id"] + "/pdf")
            assert r.status_code == 200 and hashlib.sha256(r.data).hexdigest() == f["sha256"]
            with connect(args.database) as db:
                funnel = published(db, f["group"])
            data = copy.deepcopy(funnel["data"])
            j = data["consultation_journey"]
            desired = {
                **j,
                "payment_timing": "before_scheduling",
                "preparation_material_id": material["id"],
            }
            if desired != j:
                funnel = req(
                    "POST",
                    "/admin/content/" + funnel["id"] + "/copy",
                    {"revision": funnel["revision"], "reason": why},
                )
                data["previous_version"] = funnel["data"]["previous_version"]
                data.pop("approval", None)
                data["consultation_journey"] = desired
                data["configuration_release"] = release
                funnel = req(
                    "PATCH",
                    "/admin/content/" + funnel["id"],
                    {"revision": funnel["revision"], "data": data, "reason": why},
                )
                funnel = activate(funnel)
            groups[f["group"]] = {
                "material_id": material["id"],
                "asset_id": material["data"]["asset_id"],
                "funnel_id": funnel["id"],
                "file_sha256": f["sha256"],
                "filename": f["filename"],
                "delivered_filename": funnel["data"]["consultation_journey"][
                    "preparation_filename"
                ],
                "payment_timing": funnel["data"]["consultation_journey"]["payment_timing"],
                "regular_rub": funnel["data"]["consultation_regular_rub"],
                "promo_rub": funnel["data"]["consultation_amount_rub"],
            }
        after = fingerprint()
        assert before["financial"] == after["financial"] and before["records"] == after["records"]
        assert all(after["blobs"].get(k) == v for k, v in before["blobs"].items())
        assert set(after["blobs"]) - set(before["blobs"]) <= {
            g["asset_id"] for g in groups.values()
        }
        if args.write_source:
            path = Path(app.config["FUNNEL_SOURCE_PATH"])
            source = json.loads(path.read_text())
            info = path.stat()
            for group, v in groups.items():
                source[group]["consultation_journey"].update(
                    payment_timing="before_scheduling", preparation_material_id=v["material_id"]
                )
            temp = path.with_suffix(".preparation-next")
            temp.write_text(json.dumps(source, ensure_ascii=False, indent=2))
            os.chmod(temp, info.st_mode & 0o777)
            os.chown(temp, info.st_uid, info.st_gid)
            os.replace(temp, path)
        result = {
            "release": release,
            "groups": groups,
            "financial": after["financial"],
            "existing_records_unchanged": before["records"] == after["records"],
            "old_blobs_preserved": True,
            "source_updated": args.write_source,
            "remaining_missing_pdf": "Чек-лист: Моторные вехи ребенка от 0 до 3 лет (Сенсорный календарь).pdf",
            "layout": "Текст и структура с четырёх присланных изображений; новая вёрстка PDF.",
        }
        Path(args.manifest).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        req("POST", "/logout", {})


if __name__ == "__main__":
    main()
