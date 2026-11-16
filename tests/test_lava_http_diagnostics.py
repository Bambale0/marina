"""Lava invoice failures must be diagnosable without disclosing customer data."""

import logging
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from marina import Problem
from marina.providers import invoice
from tests.test_identity import app as identity_app


@pytest.fixture
def app(tmp_path):
    return identity_app.__wrapped__(tmp_path)


def attempt():
    return {
        "id": "a" * 32,
        "data": {
            "provider": "lava",
            "email": "private-buyer@example.test",
            "offer_id": "fixture-offer",
            "currency": "RUB",
            "total": 49000,
        },
    }


def test_lava_invoice_http_400_logs_only_status_and_internal_attempt(app, monkeypatch, caplog):
    secret = "secret-key-must-not-appear"
    upstream_body = b"private-buyer@example.test upstream private diagnostic"
    app.config.update(
        LAVA_API_KEY=secret,
        HTTP_TRANSPORT=None,
        PUBLIC_URL="https://fixture.example",
        ENABLED_PAYMENT_PROVIDERS=["lava"],
    )

    def reject(request, timeout):
        raise HTTPError(request.full_url, 400, "Bad Request", None, BytesIO(upstream_body))

    monkeypatch.setattr("marina.providers.urlopen", reject)
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        with pytest.raises(Problem):
            invoice(app, attempt())

    assert "lava_http_error method=POST status=400 attempt_id=" + "a" * 32 in caplog.text
    assert secret not in caplog.text
    assert "private-buyer@example.test" not in caplog.text
    assert "upstream private diagnostic" not in caplog.text


def test_lava_transport_error_does_not_claim_provider_rejection(app, monkeypatch, caplog):
    app.config.update(
        LAVA_API_KEY="fixture-key",
        HTTP_TRANSPORT=None,
        PUBLIC_URL="https://fixture.example",
        ENABLED_PAYMENT_PROVIDERS=["lava"],
    )

    def no_response(request, timeout):
        raise URLError("simulated network timeout")

    monkeypatch.setattr("marina.providers.urlopen", no_response)
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        with pytest.raises(Problem):
            invoice(app, attempt())

    assert "lava_http_error" not in caplog.text
