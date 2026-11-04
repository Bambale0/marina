"""Follow-up expiry and file acknowledgement checkpoints through the public sale API."""

import json
from pathlib import Path

import pytest

from marina import create_app
from marina.db import connect
import marina.funnel as funnel
from tests.test_identity import app
from tests.test_funnel import private_source, setup_sale, drain, scenario
from tests.test_commerce import pay


def delivered_sale(app):
    _, owner, run, _, _, sent, files = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    return owner, run, sent, files


def followup_schedule(app, run_id):
    with connect(app.config["DATABASE"]) as db:
        return [
            (row["event"], row["available"])
            for row in db.execute(
                "SELECT event,available FROM outbox WHERE object_id=? "
                "AND event LIKE 'funnel.followup.%' ORDER BY available",
                (run_id,),
            )
        ]


@pytest.mark.parametrize("offset", [-1, 0, 1, 60, 300, 301])
def test_last_followup_and_booking_share_the_same_expiry_boundary(app, offset):
    owner, run, sent, _ = delivered_sale(app)
    drain(app)
    data = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    app.clock[0] = data["consultation_expires"] + offset
    marker = len(sent)
    drain(app)
    messages = [d["text"] for _, d in sent[marker:] if "Тестовое касание" in d.get("text", "")]
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Завтра после 18:00 МСК"}
    )
    assert booking.status_code == 200, booking.json
    assert booking.json["data"]["promotion"]["eligible"] is (offset < 0)
    assert len(messages) == (1 if offset < 0 else 0)


def test_slow_file_delivery_starts_followups_at_acknowledgement(app):
    owner, run, _, files = delivered_sale(app)
    acknowledged = []

    def slow_send(*args):
        app.clock[0] += 25
        acknowledged.append(app.clock[0])
        files.append(args)

    app.config["FILE_TRANSPORT"] = slow_send
    drain(app)
    data = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    assert len(files) == 1
    assert data["guide_file_sent_at"] == data["guide_delivered_at"] == acknowledged[0]
    assert data["consultation_expires"] == acknowledged[0] + 7 * 86400
    assert followup_schedule(app, run["id"]) == [
        (f"funnel.followup.{hours}", acknowledged[0] + hours * 3600) for hours in (24, 48, 72)
    ]


def test_restart_after_file_checkpoint_preserves_delivery_and_promo_deadline(app, monkeypatch):
    owner, run, _, files = delivered_sale(app)
    original = funnel.update
    interrupted = []

    def crash_after_file(db, obj, now, data=None, *args, **kwargs):
        if data and data.get("guide_delivered_at") and not interrupted:
            interrupted.append(True)
            raise RuntimeError("Synthetic crash after committed file checkpoint")
        return original(db, obj, now, data, *args, **kwargs)

    monkeypatch.setattr(funnel, "update", crash_after_file)
    drain(app)
    before = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    assert len(files) == 1 and before.get("guide_file_sent_at")
    assert not before.get("guide_delivered_at")
    assert followup_schedule(app, run["id"]) == []
    monkeypatch.setattr(funnel, "update", original)
    app.clock[0] += 3600
    restarted = create_app(dict(app.config))
    restarted.clock = app.clock
    drain(restarted)
    after = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    anchor = before["guide_file_sent_at"]
    assert len(files) == 1

    assert after["guide_file_sent_at"] == after["guide_delivered_at"] == anchor
    assert after["consultation_expires"] == anchor + 7 * 86400
    expected = [(f"funnel.followup.{hours}", anchor + hours * 3600) for hours in (24, 48, 72)]
    assert followup_schedule(restarted, run["id"]) == expected
    # Replaying an already completed delivery cannot move deadlines or duplicate jobs/files.
    app.clock[0] += 3600
    owner_id = owner.get("/api/me").json["account"]["id"]
    funnel.notify_funnel(restarted, owner_id, run["id"], "funnel.guide")
    final = owner.get(f"/api/funnel-runs/{run['id']}").json["data"]
    assert final["guide_delivered_at"] == anchor
    assert final["consultation_expires"] == after["consultation_expires"]
    assert followup_schedule(restarted, run["id"]) == expected
    assert len(files) == 1


def test_booking_preserves_pdf_coupon_terms_and_source_of_purchased_snapshot(app):
    source = {"material_id": "fixture-material", "pdf_sha256": "a" * 64, "page": 13}
    path = Path(app.config["FUNNEL_SOURCE_PATH"])
    data = json.loads(path.read_text())
    data["3-4"].update(
        consultation_promo_terms="Synthetic PDF terms: seven days after download.",
        consultation_promo_source=source,
    )
    path.write_text(json.dumps(data))
    owner, run, _, _ = delivered_sale(app)
    drain(app)
    from tests.test_identity import login

    scenario(login(app), consultation_promo_terms="A later published version")
    booking = owner.post(
        f"/api/funnel-runs/{run['id']}/book", json={"preferences": "Завтра вечером МСК"}
    )
    assert booking.status_code == 200, booking.json
    promo = booking.json["data"]["promotion"]
    assert promo["terms"] == data["3-4"]["consultation_promo_terms"]
    assert promo["source"] == source
    assert promo["code"] == "TEST_PROMO" and promo["amount_rub"] == 10
