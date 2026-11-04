#!/usr/bin/env python3
"""Read-only release gate for the age-screening and tripwire funnel."""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from pypdf import PdfReader

GROUPS = ("3-4", "5-6", "7-9", "10-14")
EXPECTED_BANDS = [(0, 3, "green"), (4, 7, "yellow"), (8, 12, "red")]


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    result["data"] = json.loads(result["data"])
    return result


def one_published(db, kind, json_path=None, value=None):
    if json_path is None:
        rows = db.execute(
            "SELECT * FROM objects WHERE kind=? AND status='published'", (kind,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM objects WHERE kind=? AND status='published' "
            "AND json_extract(data,?)=? ORDER BY updated DESC,rowid DESC",
            (kind, json_path, value),
        ).fetchall()
    return [unpack(row) for row in rows]


def text(value):
    return isinstance(value, str) and bool(value.strip())


def valid_method(method, group):
    if not isinstance(method, dict):
        return False
    ages = {"3-4": (36, 59), "5-6": (60, 83), "7-9": (84, 119), "10-14": (120, 179)}
    if (
        method.get("audience") != "child"
        or (method.get("min_months"), method.get("max_months")) != ages[group]
        or not all(text(method.get(k)) for k in ("source", "rights", "limitations"))
    ):
        return False
    questions = method.get("questions")
    if not isinstance(questions, list) or len(questions) != 6:
        return False
    seen = set()
    for q in questions:
        if (
            not isinstance(q, dict)
            or not text(q.get("id"))
            or q["id"] in seen
            or not text(q.get("text"))
            or q.get("when")
            or q.get("required", True) is not True
        ):
            return False
        seen.add(q["id"])
        options = q.get("options")
        if not isinstance(options, list) or len(options) != 3:
            return False
        ids = set()
        for score, option in enumerate(options):
            if (
                not isinstance(option, dict)
                or not text(option.get("id"))
                or option["id"] in ids
                or not text(option.get("text"))
                or type(option.get("score")) is not int
                or option["score"] != score
            ):
                return False
            ids.add(option["id"])
    bands = method.get("bands")
    return (
        isinstance(bands, list)
        and len(bands) == 3
        and all(
            isinstance(b, dict)
            and type(b.get("min")) is int
            and type(b.get("max")) is int
            and (b["min"], b["max"], b.get("zone")) == expected
            and text(b.get("text"))
            for b, expected in zip(bands, EXPECTED_BANDS)
        )
    )


def check_material(db, material_id, prefix, errors, expected_pages=None, allow_video=False):
    row = db.execute(
        "SELECT * FROM objects WHERE id=? AND kind='material'", (material_id,)
    ).fetchone()
    material = unpack(row)
    if not material or material["status"] != "published":
        errors.append(prefix + ":material_not_published")
        return None
    blob = db.execute(
        "SELECT * FROM blobs WHERE id=?", (material["data"].get("asset_id"),)
    ).fetchone()
    if not blob or blob["case_id"] != material_id:
        errors.append(prefix + ":blob_association")
        return None
    digest = hashlib.sha256(blob["body"]).hexdigest()
    if digest != blob["digest"]:
        errors.append(prefix + ":pdf_digest_mismatch")
    if allow_video and blob["mime"] == "video/mp4":
        if not blob["body"]:
            errors.append(prefix + ":video_empty")
        return None
    if blob["mime"] != "application/pdf":
        errors.append(prefix + ":pdf_invalid")
        return None
    try:
        reader = PdfReader(io.BytesIO(blob["body"]), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted")
        page_count = len(reader.pages)
        if not page_count:
            raise ValueError("empty")
        for page in reader.pages:
            _ = page.mediabox
            content = page.get_contents()
            if content is not None:
                content.get_data()
    except Exception:
        errors.append(prefix + ":pdf_invalid")
        return None
    if expected_pages is not None and page_count != expected_pages:
        errors.append(f"{prefix}:pdf_pages:{page_count}")
    return {
        "material_id": material_id,
        "pdf_pages": page_count,
        "pdf_size": len(blob["body"]),
        "pdf_sha256": digest,
    }


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", default=os.environ.get("DATABASE", "/var/lib/marina/marina.sqlite")
    )
    parser.add_argument("--expected-amount-kopecks", type=positive_int)
    parser.add_argument("--expected-pages", type=positive_int, default=14)
    parser.add_argument("--expected-tripwire-delay", type=int)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--require-environment", action="store_true")
    args = parser.parse_args()
    # Explicit files are authoritative; do not implicitly discover or load .env.
    environment = dict(os.environ)
    env_file_missing = args.env_file is not None and not args.env_file.is_file()
    if args.env_file and not env_file_missing:
        environment = dict(dotenv_values(args.env_file, interpolate=False))
    db = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    errors, warnings, groups = [], [], []

    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        errors.append("database_integrity")

    privacy = one_published(db, "policy", "$.type", "privacy")
    if not privacy or not text(privacy[0]["data"].get("terms")):
        errors.append("privacy_policy_missing")

    offers = [obj for obj in one_published(db, "template") if obj["data"].get("type") == "offer"]
    policies = [
        obj
        for obj in one_published(db, "policy")
        if obj["data"].get("type") == "commerce" and obj["data"].get("scope") == "guide"
    ]
    if len(offers) != 1:
        errors.append(f"published_offer_count:{len(offers)}")
    if len(policies) != 1:
        errors.append(f"guide_policy_count:{len(policies)}")
    if offers and policies and policies[0]["data"].get("offer_template_id") != offers[0]["id"]:
        errors.append("guide_policy_offer_not_current")
    if policies and policies[0]["data"].get("quote_ttl_seconds") != 7200:
        errors.append("guide_quote_ttl_not_two_hours")

    profiles = db.execute(
        "SELECT * FROM objects WHERE kind='bot_profile' ORDER BY created DESC,rowid DESC LIMIT 1"
    ).fetchall()
    profile = unpack(profiles[0]) if profiles else None
    if not profile or profile["status"] != "applied":
        errors.append("bot_profile_not_applied")
    elif "реальные причины" not in profile["data"].get("description", ""):
        errors.append("bot_profile_copy_not_current")

    for group in GROUPS:
        funnels = one_published(db, "funnel", "$.age_group", group)
        if len(funnels) != 1:
            errors.append(f"{group}:published_funnel_count:{len(funnels)}")
            continue
        funnel = funnels[0]
        data = funnel["data"]
        if not valid_method(data.get("method"), group):
            errors.append(f"{group}:method_invalid")
        delay = data.get("tripwire_delay_seconds")
        if (
            type(delay) is not int
            or not 0 <= delay <= 86400
            or (args.expected_tripwire_delay is not None and delay != args.expected_tripwire_delay)
        ):
            errors.append(f"{group}:tripwire_delay")
        followups = data.get("followups")
        if (
            not isinstance(followups, list)
            or len(followups) != 3
            or not all(
                isinstance(f, dict)
                and type(f.get("hours")) is int
                and f["hours"] == hours
                and text(f.get("text"))
                for f, hours in zip(followups, (24, 48, 72))
            )
        ):
            errors.append(f"{group}:followups")
        for key in ("channel_url", "contact_url"):
            url = data.get(key)
            if (
                not isinstance(url, str)
                or urlparse(url).scheme != "https"
                or urlparse(url).hostname != "t.me"
                or not urlparse(url).path.strip("/")
            ):
                errors.append(f"{group}:contact_links:{key}")
        pages = data.get("guide_pages")
        if (
            not isinstance(pages, list)
            or len(pages) != args.expected_pages
            or not all(
                isinstance(p, dict) and text(p.get("title")) and text(p.get("text")) for p in pages
            )
        ):
            errors.append(f"{group}:guide_metadata_pages")
        for key, label in (("free_material_id", "free"), ("refusal_material_id", "refusal")):
            if not data.get(key):
                target = warnings if group == "5-6" and label == "refusal" else errors
                target.append(f"{group}:{label}_material_missing")
            else:
                check_material(
                    db, data[key], f"{group}:{label}", errors, allow_video=label == "refusal"
                )

        product_row = db.execute(
            "SELECT * FROM objects WHERE id=? AND kind='product'", (data.get("guide_product_id"),)
        ).fetchone()
        product = unpack(product_row)
        if not product or product["status"] != "published":
            errors.append(f"{group}:product_not_published")
            continue
        product_data = product["data"]
        if (
            product_data.get("service") != "guide"
            or product_data.get("age_group") != group
            or type(product_data.get("amount")) is not int
            or product_data.get("amount", 0) <= 0
            or (
                args.expected_amount_kopecks is not None
                and product_data.get("amount") != args.expected_amount_kopecks
            )
            or product_data.get("currency") != "RUB"
            or product_data.get("web_sales_approved") is not True
            or not product_data.get("lava_offer_id")
        ):
            errors.append(f"{group}:product_contract")

        result = check_material(
            db, product_data.get("material_id"), group, errors, expected_pages=args.expected_pages
        )
        if result:
            groups.append(
                {"group": group, "funnel_id": funnel["id"], "product_id": product["id"], **result}
            )

    flags = {
        "DEV_TRIPWIRE_BYPASS_PAYMENT": "tripwire_payment_bypass_enabled",
        "DEV_OFFER_BYPASSES_CONSENT": "offer_consent_bypass_enabled",
    }
    known = True
    for key, error in flags.items():
        value = (environment.get(key) or "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            errors.append(error)
        elif value not in {"0", "false", "no", "off"}:
            known = False
            (errors if args.require_environment else warnings).append(
                "environment_flag_unknown:" + key
            )
    if env_file_missing:
        errors.append("env_file_missing")
    environment_verified = known and not env_file_missing
    if args.require_environment and not environment_verified:
        errors.append("environment_not_verified")
    provider_keys = {
        key: "present" if text(environment.get(key)) else "missing"
        for key in ("LAVA_API_KEY", "LAVA_WEBHOOK_SECRET")
    }
    for key, status in provider_keys.items():
        if status == "missing":
            (errors if args.require_environment else warnings).append("provider_key_missing:" + key)

    unresolved = db.execute(
        "SELECT o.id,o.owner,a.role FROM objects o LEFT JOIN accounts a ON a.id=o.owner "
        "WHERE o.kind='reconciliation' AND o.status='open'"
    ).fetchall()
    for row in unresolved:
        target = errors if row["role"] != "admin" else warnings
        target.append(f"open_reconciliation:{row['id']}:{row['role'] or 'unknown'}")

    db.close()
    report = {
        "scope": "configuration_only",
        "ok": not errors,
        "configuration_ok": not errors,
        "runtime_verified": False,
        "commercial_ready": False,
        "environment_verified": environment_verified,
        "provider_keys": provider_keys,
        "errors": errors,
        "warnings": warnings,
        "groups": groups,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
