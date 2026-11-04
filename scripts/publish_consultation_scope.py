"""Publish an externally supplied, reviewed configuration through audited admin APIs.

Run on a backup copy first. Credentials are read from the explicit environment file;
the input and output contain no credentials. No invoice or message is sent.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from marina import create_app
from marina.bot import trusted_client
from marina.db import connect, unpack
from marina.providers import lava


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--write-source", action="store_true")
    args = parser.parse_args()
    cfg = json.loads(args.input.read_text())
    load_dotenv(args.env_file, override=True)
    app = create_app({"DATABASE": str(args.database), "DEV_OFFER_BYPASSES_CONSENT": False})
    why = cfg["reason"]

    def state():
        with connect(args.database) as db:
            financial = {
                t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("receipts", "fulfillments", "intent_fulfillments")
            }
            rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT id,kind,status,revision,data FROM objects WHERE kind IN ('order','right','booking','funnel_run') ORDER BY id"
                )
            ]
            blobs = [
                tuple(r) for r in db.execute("SELECT id,digest,length(body) FROM blobs ORDER BY id")
            ]
        return {
            "financial": financial,
            "existing_records": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            "blobs": hashlib.sha256(json.dumps(blobs).encode()).hexdigest(),
        }

    before = state()
    with connect(args.database) as db:
        admins = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM accounts WHERE role='admin' AND blocked=0 ORDER BY created,id"
            )
            if r["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"]
        ]
    assert admins, "No configured existing administrator"
    client, _ = trusted_client(
        app, {"id": admins[0]["telegram_id"], "first_name": admins[0]["name"]}
    )

    def request(method, path, data=None):
        response = client.open("/api" + path, method=method, json=data)
        assert response.status_code < 400, (
            method,
            path,
            response.status_code,
            response.json.get("error"),
        )
        return response.json

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

    def publish(obj, data):
        payload = copy.deepcopy(data)
        kind = payload.pop("_kind", None)
        payload.pop("approval", None)
        payload["configuration_release"] = cfg["release_key"]
        if (
            obj
            and obj["status"] == "published"
            and all(obj["data"].get(k) == v for k, v in payload.items())
        ):
            return obj
        if obj and obj["status"] == "published":
            obj = request(
                "POST",
                "/admin/content/" + obj["id"] + "/copy",
                {"revision": obj["revision"], "reason": why},
            )
            payload["previous_version"] = obj["data"]["previous_version"]
        if obj is None:
            obj = request(
                "POST",
                "/admin/content",
                {
                    "kind": kind,
                    "title": payload["title"],
                    "data": payload,
                    "reason": why,
                },
            )
        else:
            assert obj["status"] == "draft", "Only reviewed drafts may be edited"
            obj = request(
                "PATCH",
                "/admin/content/" + obj["id"],
                {
                    "revision": obj["revision"],
                    "title": payload["title"],
                    "data": payload,
                    "reason": why,
                },
            )
        for action in ("approve", "publish"):
            obj = request(
                "POST",
                "/admin/content/" + obj["id"] + "/" + action,
                {"revision": obj["revision"], "reason": why},
            )
        assert obj["status"] == "published"
        return obj

    try:
        provider_items = lava(app, "GET", "/api/v2/products?feedVisibility=ALL")["items"]
        for group, target in cfg["products"].items():
            p = next(p for p in provider_items if p["id"] == target["lava_product_id"])
            assert p.get("isDynamicPrice") is True and p["type"] == "CONSULTATION", group
            assert target["lava_offer_id"] in {o["id"] for o in p["offers"]}, group

        offer = current("template", type="offer")
        assert offer
        offer_data = copy.deepcopy(offer["data"])
        if offer_data.get("configuration_release") != cfg["release_key"]:
            for old, new in cfg["offer_replacements"]:
                assert old in offer_data["text"], "Offer changed; review replacement again"
                offer_data["text"] = offer_data["text"].replace(old, new)
            offer_data["text"] = cfg["offer_scope"] + "\n\n" + offer_data["text"]
            assert "[указать" not in offer_data["text"]
            offer_data["title"] = cfg["offer_title"]
            offer = publish(offer, offer_data)

        guide_policy = current("policy", type="commerce", scope="guide")
        assert guide_policy
        guide_data = {**guide_policy["data"], "offer_template_id": offer["id"]}
        guide_policy = publish(guide_policy, guide_data)
        consultation_data = {
            **guide_policy["data"],
            "scope": "consultation",
            "title": cfg["consultation_policy_title"],
            "terms": cfg["consultation_terms"],
            "offer_template_id": offer["id"],
        }
        consultation_data.pop("previous_version", None)
        consultation_data["_kind"] = "policy"
        old = current("policy", type="commerce", scope="consultation")
        consultation_policy = publish(old, consultation_data)

        privacy_data = {**cfg["privacy"], "_kind": "policy"}
        privacy_policy = publish(current("policy", type="privacy"), privacy_data)
        operations_data = {**cfg["operations"], "_kind": "policy"}
        operations_policy = publish(current("policy", type="operations"), operations_data)
        results = {}
        for group, target in cfg["products"].items():
            obj = request("GET", "/admin/content/" + target["marina_product_id"])
            funnel = current("funnel", age_group=group)
            assert funnel
            assert funnel["data"]["consultation_regular_rub"] == target["regular_rub"]
            assert funnel["data"]["consultation_amount_rub"] == target["promo_rub"]
            assert funnel["data"]["consultation_code"] == target["code"]
            material_source = funnel["data"]["consultation_promo_source"]
            with connect(args.database) as db:
                row = db.execute(
                    "SELECT data FROM objects WHERE id=?", (material_source["material_id"],)
                ).fetchone()
                asset = json.loads(row[0])["asset_id"]
                blob = db.execute("SELECT body,digest FROM blobs WHERE id=?", (asset,)).fetchone()
            assert (
                hashlib.sha256(blob["body"]).hexdigest()
                == material_source["pdf_sha256"]
                == blob["digest"]
            )
            product_data = {
                **obj["data"],
                "amount": target["regular_rub"] * 100,
                "currency": "RUB",
                "service": "booking",
                "lava_offer_id": target["lava_offer_id"],
                "lava_product_id": target["lava_product_id"],
                "web_sales_approved": True,
                "launch_approved": True,
                "terms": target["terms"],
            }
            product = publish(obj, product_data)
            funnel = publish(funnel, {**funnel["data"], "consultation_product_id": product["id"]})
            results[group] = {
                "product_id": product["id"],
                "funnel_id": funnel["id"],
                "lava_product_id": target["lava_product_id"],
                "lava_offer_id": target["lava_offer_id"],
                "regular_rub": target["regular_rub"],
                "promo_rub": target["promo_rub"],
                "code": target["code"],
            }

        after = state()
        assert before == after, "Existing financial facts, runs or materials changed"
        if args.write_source:
            source_path = Path(app.config["FUNNEL_SOURCE_PATH"])
            source = json.loads(source_path.read_text())
            for group, result in results.items():
                source[group]["consultation_product_id"] = result["product_id"]
            info = source_path.stat()
            temporary = source_path.with_suffix(".next")
            temporary.write_text(json.dumps(source, ensure_ascii=False, indent=2))
            os.chmod(temporary, info.st_mode & 0o777)
            os.chown(temporary, info.st_uid, info.st_gid)
            os.replace(temporary, source_path)
        manifest = {
            "release_key": cfg["release_key"],
            "products": results,
            "offer_id": offer["id"],
            "guide_policy_id": guide_policy["id"],
            "consultation_policy_id": consultation_policy["id"],
            "privacy_policy_id": privacy_policy["id"],
            "operations_policy_id": operations_policy["id"],
            "before": before,
            "after": after,
            "source_updated": args.write_source,
        }
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        os.chmod(args.manifest, 0o600)
        print(json.dumps(manifest, ensure_ascii=False))
    finally:
        request("POST", "/logout", {})


if __name__ == "__main__":
    main()
