"""Paid appointments produce a bot receipt and cannot start before payment."""

import pytest

from marina.operations import notify
from tests.test_consultation_commerce import (
    app,
    confirmed_consultation,
    pay_consultation,
    quote,
)
from tests.test_funnel import drain


@pytest.mark.parametrize("action", ["connection", "complete"])
def test_commercial_consultation_requires_payment_before_service(app, action):
    admin, _, _, booking, *_ = confirmed_consultation(app)
    response = admin.post(
        f"/api/admin/bookings/{booking['id']}/{action}",
        json={
            "revision": booking["revision"],
            "reason": "Synthetic delivery",
            "text": "https://meet.example.test/appointment",
        },
    )
    assert response.status_code == 422, response.json


def test_paid_consultation_bot_receipt_and_service_access(app):
    admin, owner, _, booking, _, _, sent, _ = confirmed_consultation(app)
    calculation = quote(owner, booking)
    order = owner.post("/api/orders", json={"quote_id": calculation.json["id"], "key": "receipt"})
    assert order.status_code == 200, order.json
    assert pay_consultation(app, owner, order.json).status_code == 200
    drain(app)
    client = [
        payload["text"]
        for _, payload in sent
        if payload.get("chat_id") == 2 and "Консультация оплачена" in payload.get("text", "")
    ]
    assert len(client) == 1
    assert "3900,00 ₽" in client[0] and "оплачена" in client[0].lower()
    assert any(payload.get("chat_id") == 1 for _, payload in sent)
    sent.clear()
    notify(app, booking["owner"], booking["id"], "consultation.paid")
    assert sent == []
    paid = owner.get(f"/api/bookings/{booking['id']}").json
    response = admin.post(
        f"/api/admin/bookings/{booking['id']}/connection",
        json={
            "revision": paid["revision"],
            "reason": "Synthetic paid appointment",
            "text": "https://meet.example.test/appointment",
        },
    )
    assert response.status_code == 200, response.json
