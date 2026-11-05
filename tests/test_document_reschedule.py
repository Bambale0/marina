"""The author's free reschedule keeps payment and replaces the obsolete appointment."""

import pytest

from tests.test_consultation_commerce import app, pay_consultation, quote
from tests.test_consultation_remainder import journey_sale


@pytest.mark.parametrize(
    "group,base,promo",
    [("3-4", 600000, 3900), ("5-6", 600000, 3900), ("7-9", 650000, 3900), ("10-14", 700000, 4500)],
)
def test_paid_consultation_can_move_without_new_charge_or_obsolete_meeting_link(
    app, group, base, promo
):
    admin, owner, _, booking, _, _, _ = journey_sale(app, group, base, promo)
    priced = quote(owner, booking)
    assert priced.status_code == 201, priced.json
    order = owner.post("/api/orders", json={"quote_id": priced.json["id"], "key": "reschedule"})
    assert order.status_code == 200, order.json
    assert pay_consultation(app, owner, order.json).status_code == 200
    id = booking["id"]
    paid = owner.get("/api/bookings/" + id).json
    payment_before = admin.get("/api/admin/objects/" + order.json["id"]).json

    def propose(current, seconds):
        response = admin.post(
            "/api/admin/bookings/" + id + "/propose",
            json={
                "revision": current["revision"],
                "starts": app.clock[0] + seconds,
                "duration_minutes": current["data"]["journey_snapshot"]["session_minutes"],
                "timezone": "Europe/Moscow",
                "reason": "Family requested another suitable day",
            },
        )
        assert response.status_code == 200, response.json
        return response.json

    proposed = propose(paid, 86400)
    original_proposal = proposed["data"]["current_proposal"]
    confirmed = owner.post(
        "/api/bookings/" + id + "/confirm", json={"proposal_id": original_proposal}
    )
    assert confirmed.status_code == 200, confirmed.json
    linked = admin.post(
        "/api/admin/bookings/" + id + "/connection",
        json={
            "revision": confirmed.json["revision"],
            "text": "https://meeting.example/old",
            "reason": "Specialist prepared first meeting",
        },
    )
    assert linked.status_code == 200, linked.json
    moved = owner.post("/api/bookings/" + id + "/reschedule", json={})
    assert moved.status_code == 200, moved.json
    assert moved.json["status"] == "reschedule_requested"
    assert moved.json["data"].get("connection") is None
    replay = owner.post("/api/bookings/" + id + "/reschedule", json={})
    assert replay.status_code == 200, replay.json
    assert replay.json["revision"] == moved.json["revision"]
    replacement = propose(replay.json, 172800)
    assert (
        owner.post(
            "/api/bookings/" + id + "/confirm", json={"proposal_id": original_proposal}
        ).status_code
        == 409
    )
    final = owner.post(
        "/api/bookings/" + id + "/confirm",
        json={"proposal_id": replacement["data"]["current_proposal"]},
    )
    assert final.status_code == 200, final.json
    assert final.json["status"] == "confirmed"
    assert final.json["data"]["paid_at"] == paid["data"]["paid_at"]
    assert final.json["data"]["order_id"] == paid["data"]["order_id"]
    assert quote(owner, final.json).status_code == 409
    payment_after = admin.get("/api/admin/objects/" + order.json["id"]).json
    assert payment_after["object"] == payment_before["object"]
    assert payment_after["receipts"] == payment_before["receipts"]
    assert len(payment_after["receipts"]) == 1
