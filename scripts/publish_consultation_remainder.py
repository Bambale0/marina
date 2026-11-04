"""Publish the reviewed remainder through audited APIs; rehearse on a copy first."""

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
from marina.db import connect, unpack


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
    app = create_app({"DATABASE": args.database, "DEV_OFFER_BYPASSES_CONSENT": False})
    release = "consultation-remainder-20261105"
    why = "Владелец потребовал корректировки из ДЛЯ БОТА ОСТАТОК.pdf и уточнил: задание → видео выполнения ребёнком → ручная коррекция и рекомендации специалиста."

    def fingerprint():
        with connect(args.database) as db:
            counts = {
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
            "financial": counts,
            "records": hashlib.sha256(json.dumps(records).encode()).hexdigest(),
            "blobs": hashlib.sha256(json.dumps(blobs).encode()).hexdigest(),
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

    def current(kind, **fields):
        with connect(args.database) as db:
            rows = [
                unpack(r)
                for r in db.execute(
                    "SELECT * FROM objects WHERE kind=? AND status='published' ORDER BY updated DESC,rowid DESC",
                    (kind,),
                )
            ]
        return next(
            (r for r in rows if all(r["data"].get(k) == v for k, v in fields.items())), None
        )

    def publish(obj, kind, data):
        payload = copy.deepcopy(data)
        payload.pop("approval", None)
        payload["configuration_release"] = release
        if (
            obj
            and obj["status"] == "published"
            and all(obj["data"].get(k) == v for k, v in payload.items())
        ):
            return obj
        if obj and obj["status"] == "published":
            obj = req(
                "POST",
                "/admin/content/" + obj["id"] + "/copy",
                {"revision": obj["revision"], "reason": why},
            )
            payload["previous_version"] = obj["data"]["previous_version"]
        if obj:
            assert obj["status"] == "draft"
            obj = req(
                "PATCH",
                "/admin/content/" + obj["id"],
                {
                    "revision": obj["revision"],
                    "title": payload["title"],
                    "data": payload,
                    "reason": why,
                },
            )
        else:
            obj = req(
                "POST",
                "/admin/content",
                {"kind": kind, "title": payload["title"], "data": payload, "reason": why},
            )
        for action in ("approve", "publish"):
            obj = req(
                "POST",
                "/admin/content/" + obj["id"] + "/" + action,
                {"revision": obj["revision"], "reason": why},
            )
        return obj

    try:
        old = current("template", type="offer")
        assert old
        offer_data = copy.deepcopy(old["data"])
        prefix = "Сфера новых предложений: возрастные PDF и онлайн-диагностика, видеоразбор выполнения задания специалиста для малышей и личные обращения старших подростков и взрослых. Подписка и автоматический тренажёр не предлагаются. Условия конкретной услуги согласуются в её опубликованном предложении; прежние оплаченные обязательства и финансовые факты сохраняются."
        if not offer_data["text"].startswith(prefix):
            previous = json.loads(
                Path(
                    "/var/lib/marina/content/consultation-activation-input-20261105.json"
                ).read_text()
            )["offer_scope"]
            assert offer_data["text"].startswith(previous), "Offer scope changed; review explicitly"
            offer_data["text"] = prefix + offer_data["text"][len(previous) :]
        offer = publish(old, "template", offer_data)
        for scope in ("guide", "consultation"):
            obj = current("policy", type="commerce", scope=scope)
            assert obj
            publish(obj, "policy", {**obj["data"], "offer_template_id": offer["id"]})
        privacy = current("policy", type="privacy")
        assert privacy
        root = Path(__file__).resolve().parents[1]
        privacy = publish(
            privacy,
            "policy",
            {
                **privacy["data"],
                "title": "Политика данных: диагностика и задания с видео — 05.11.2026",
                "terms": (root / "docs/legal/privacy-policy.txt").read_text(),
                "consent_text": (root / "docs/legal/privacy-consent.txt").read_text(),
            },
        )
        routes = publish(current("policy", type="consultation_routes"), "policy", spec["outside"])
        ops = current("policy", type="operations")
        assert ops
        services = list(ops["data"].get("enabled_services", ["pdf", "course", "booking", "guide"]))
        if "video_task" not in services:
            services.append("video_task")
        ops = publish(ops, "policy", {**ops["data"], "enabled_services": services})
        groups = {}
        missing = []
        for group, j in spec["ages"].items():
            obj = current("funnel", age_group=group)
            assert obj
            assert not j["preparation_material_id"]
            # The author-supplied preparation PDFs are not attached yet. Do not claim delivery
            # or enable the pay-first promise with a substituted paid guide.
            j = copy.deepcopy(j)
            j["payment_timing"] = "after_scheduling"
            missing.append({"age_group": group, "filename": j["preparation_filename"]})
            funnel = publish(obj, "funnel", {**obj["data"], "consultation_journey": j})
            groups[group] = {
                "funnel_id": funnel["id"],
                "payment_timing": j["payment_timing"],
                "regular_rub": obj["data"]["consultation_regular_rub"],
                "promo_rub": obj["data"]["consultation_amount_rub"],
            }
        missing.append(
            {
                "route": "baby_0_2",
                "filename": spec["outside"]["routes"]["baby_0_2"]["material_filename"],
            }
        )
        after = fingerprint()
        assert before == after, "Existing financial facts, records or BLOB changed"
        if args.write_source:
            path = Path(app.config["FUNNEL_SOURCE_PATH"])
            source = json.loads(path.read_text())
            for group, j in spec["ages"].items():
                source[group]["consultation_journey"] = {**j, "payment_timing": "after_scheduling"}
            info = path.stat()
            tmp = path.with_suffix(".next")
            tmp.write_text(json.dumps(source, ensure_ascii=False, indent=2))
            os.chmod(tmp, info.st_mode & 0o777)
            os.chown(tmp, info.st_uid, info.st_gid)
            os.replace(tmp, path)
        result = {
            "release": release,
            "offer_id": offer["id"],
            "privacy_id": privacy["id"],
            "routes_policy_id": routes["id"],
            "operations_policy_id": ops["id"],
            "groups": groups,
            "missing_author_pdfs": missing,
            "before": before,
            "after": after,
            "source_updated": args.write_source,
            "video_task_payment": "личное согласование со специалистом; автоматическое поступление не создаётся",
        }
        Path(args.manifest).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        req("POST", "/logout", {})


if __name__ == "__main__":
    main()
