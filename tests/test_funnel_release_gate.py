"""Public CLI checks using synthetic SQLite and reportlab PDFs only."""

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from reportlab.pdfgen.canvas import Canvas

from marina.db import SCHEMA
from tests.funnel_fixtures import configure_funnel_source

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_funnel_release.py"


@pytest.fixture
def gate(tmp_path):
    app = SimpleNamespace(config={})
    configure_funnel_source(app, tmp_path)
    scenarios = json.loads(Path(app.config["FUNNEL_SOURCE_PATH"]).read_text())
    output = io.BytesIO()
    canvas = Canvas(output)
    for _ in range(14):
        canvas.drawString(20, 20, "Synthetic test fixture")
        canvas.showPage()
    canvas.save()
    pdf = output.getvalue()
    path = tmp_path / "synthetic.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)

    def put(id, kind, data, status="published"):
        db.execute(
            "INSERT INTO objects(id,kind,status,data,created,updated) VALUES(?,?,?,?,0,0)",
            (id, kind, status, json.dumps(data)),
        )

    put("offer", "template", {"type": "offer"})
    put("privacy", "policy", {"type": "privacy", "terms": "Synthetic terms"})
    put(
        "commerce",
        "policy",
        {
            "type": "commerce",
            "scope": "guide",
            "offer_template_id": "offer",
            "quote_ttl_seconds": 7200,
        },
    )
    put("profile", "bot_profile", {"description": "реальные причины"}, "applied")
    for group, data in scenarios.items():
        data.update(
            guide_product_id="p" + group,
            free_material_id="m" + group,
            refusal_material_id="m" + group,
            channel_url="https://t.me/synthetic",
            contact_url="https://t.me/synthetic",
        )
        put("f" + group, "funnel", data)
        put(
            "p" + group,
            "product",
            {
                "service": "guide",
                "age_group": group,
                "amount": 67890,
                "currency": "RUB",
                "web_sales_approved": True,
                "lava_offer_id": "synthetic",
                "material_id": "m" + group,
            },
        )
        put("m" + group, "material", {"asset_id": "b" + group})
        db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                "b" + group,
                "synthetic",
                "synthetic",
                "m" + group,
                "application/pdf",
                hashlib.sha256(pdf).hexdigest(),
                pdf,
                0,
            ),
        )
    db.commit()
    envfile = tmp_path / "explicit.env"
    envfile.write_text(
        "DEV_TRIPWIRE_BYPASS_PAYMENT=0\nDEV_OFFER_BYPASSES_CONSENT=0\n"
        "LAVA_API_KEY=synthetic-secret\nLAVA_WEBHOOK_SECRET=synthetic-secret\n"
    )

    def run(*args, environment=True):
        db.commit()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DEV_", "LAVA_"))}
        command = [sys.executable, str(SCRIPT), "--database", str(path)]
        if environment:
            command += ["--env-file", str(envfile)]
        result = subprocess.run(command + list(args), env=env, capture_output=True, text=True)
        assert "synthetic-secret" not in result.stdout + result.stderr
        return result.returncode, json.loads(result.stdout)

    yield db, run, envfile
    db.close()


def change(db, id, **fields):
    data = json.loads(db.execute("SELECT data FROM objects WHERE id=?", (id,)).fetchone()[0])
    data.update(fields)
    db.execute("UPDATE objects SET data=? WHERE id=?", (json.dumps(data), id))


def test_valid_pdf_dynamic_price_configuration_only(gate):
    _, run, _ = gate
    code, report = run("--require-environment")
    assert code == 0, report
    assert report["ok"] is report["configuration_ok"] is True
    assert report["scope"] == "configuration_only"
    assert report["runtime_verified"] is report["commercial_ready"] is False
    assert all(group["pdf_pages"] == 14 for group in report["groups"])


@pytest.mark.parametrize("fault", ["fake", "digest", "association"])
def test_rejects_invalid_pdf_or_blob(gate, fault):
    db, run, _ = gate
    if fault == "fake":
        body = b"%PDF-" + b" /Type /Page" * 14
        db.execute("UPDATE blobs SET body=?,digest=?", (body, hashlib.sha256(body).hexdigest()))
    elif fault == "digest":
        db.execute("UPDATE blobs SET digest='arbitrary'")
    else:
        db.execute("UPDATE blobs SET case_id='wrong'")
    code, report = run()
    assert code == 1
    assert report["errors"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("free_material_id", "", "free_material_missing"),
        ("refusal_material_id", "", "refusal_material_missing"),
        ("contact_url", "", "contact_links"),
        ("followups", [{"hours": 24, "text": ""}], "followups"),
    ],
)
def test_missing_required_configuration(gate, field, value, error):
    db, run, _ = gate
    change(db, "f3-4", **{field: value})
    code, report = run()
    assert code == 1
    assert any(error in item for item in report["errors"])


def test_wrong_product_group(gate):
    db, run, _ = gate
    change(db, "p3-4", age_group="5-6")
    assert run()[0] == 1


def test_missing_privacy(gate):
    db, run, _ = gate
    db.execute("DELETE FROM objects WHERE id='privacy'")
    code, report = run()
    assert code == 1
    assert any("privacy" in error for error in report["errors"])


@pytest.mark.parametrize("flag", ["DEV_TRIPWIRE_BYPASS_PAYMENT", "DEV_OFFER_BYPASSES_CONSENT"])
def test_explicit_environment_bypass_fails(gate, flag):
    _, run, envfile = gate
    with envfile.open("a") as stream:
        stream.write(flag + "=1\n")
    assert run()[0] == 1


def test_environment_required_and_provider_status(gate):
    _, run, envfile = gate
    code, report = run("--require-environment", environment=False)
    assert code == 1
    assert "environment_not_verified" in report["errors"]
    envfile.write_text("DEV_TRIPWIRE_BYPASS_PAYMENT=0\nDEV_OFFER_BYPASSES_CONSENT=0\n")
    code, report = run("--require-environment")
    assert code == 1
    assert report["provider_keys"]["LAVA_API_KEY"] == "missing"


def test_optional_expectations(gate):
    _, run, _ = gate
    assert run("--expected-amount-kopecks", "49000")[0] == 1
    assert run("--expected-tripwire-delay", "2")[0] == 1
    assert run("--expected-amount-kopecks", "67890", "--expected-tripwire-delay", "180")[0] == 0
    assert run("--expected-pages", "13")[0] == 1


@pytest.mark.parametrize("part", ["limitations", "questions", "bands"])
def test_invalid_method(gate, part):
    db, run, _ = gate
    method = json.loads(db.execute("SELECT data FROM objects WHERE id='f3-4'").fetchone()[0])[
        "method"
    ]
    if part == "limitations":
        method[part] = ""
    elif part == "questions":
        method[part][0]["options"][0]["id"] = "b"
    else:
        method[part][0]["max"] = 4
    change(db, "f3-4", method=method)
    assert run()[0] == 1


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "49000"])
def test_price_must_be_positive_integer(gate, amount):
    db, run, _ = gate
    change(db, "p3-4", amount=amount)
    assert run()[0] == 1


@pytest.mark.parametrize(
    "kind", ["service", "material_unpublished", "free_missing_blob", "refusal_missing_blob"]
)
def test_invalid_material_and_service_links(gate, kind):
    db, run, _ = gate
    if kind == "service":
        change(db, "p3-4", service="case")
    elif kind == "material_unpublished":
        db.execute("UPDATE objects SET status='draft' WHERE id='m3-4'")
    else:
        field = "free_material_id" if kind.startswith("free") else "refusal_material_id"
        change(db, "f3-4", **{field: "missing"})
    assert run()[0] == 1


def test_configurable_pdf_page_count(gate):
    db, run, _ = gate
    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.drawString(20, 20, "Synthetic single page")
    canvas.showPage()
    canvas.save()
    pdf = output.getvalue()
    db.execute("UPDATE blobs SET body=?,digest=?", (pdf, hashlib.sha256(pdf).hexdigest()))
    for group in ("3-4", "5-6", "7-9", "10-14"):
        change(db, "f" + group, guide_pages=[{"title": "Synthetic", "text": "Fixture"}])
    assert run("--expected-pages", "1")[0] == 0
    assert run()[0] == 1


def test_optional_environment_reports_unknowns_without_certification(gate):
    _, run, _ = gate
    code, report = run(environment=False)
    assert code == 0
    assert report["environment_verified"] is False
    assert report["commercial_ready"] is report["runtime_verified"] is False
    assert "provider_key_missing:LAVA_API_KEY" in report["warnings"]
    assert "environment_flag_unknown:DEV_OFFER_BYPASSES_CONSENT" in report["warnings"]


def test_optional_preschool_refusal_material_is_only_a_warning(gate):
    db, run, _ = gate
    change(db, "f5-6", refusal_material_id="")
    code, report = run()
    assert code == 0
    assert "5-6:refusal_material_missing" in report["warnings"]
