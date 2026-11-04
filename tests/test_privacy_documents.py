from tests.test_identity import app, login
from tests.test_screening import publish


def privacy(admin):
    return publish(
        admin,
        "policy",
        {
            "type": "privacy",
            "terms": "Published policy revision",
            "consent_text": "Separate voluntary processing consent",
            "require_representative_confirmation": True,
            "require_marketing_consent": True,
            "media_max_bytes": 10000,
            "retention_days": 30,
        },
    )


def test_separate_consent_retains_exact_text_and_requires_representative(app):
    admin, owner = login(app), login(app, 2)
    p = privacy(admin)
    payload = {"policy_id": p["id"], "accepted": True}
    assert owner.post("/api/consents", json=payload).status_code == 422
    response = owner.post("/api/consents", json={**payload, "representative_confirmed": True})
    assert response.status_code == 200, response.json
    assert response.json["data"]["consent_text_snapshot"] == p["data"]["consent_text"]
    assert response.json["data"]["policy_revision"] == p["revision"]
    assert len(response.json["data"]["policy_digest"]) == 64
    assert owner.get("/api/privacy").json["accepted"] is True


def test_followup_delivery_requires_independent_marketing_choice(app, tmp_path):
    from tests.test_funnel import setup_sale, drain
    from tests.test_commerce import pay
    from tests.funnel_fixtures import configure_funnel_source

    configure_funnel_source(app, tmp_path)
    admin, owner, run, _, _, sent, _ = setup_sale(app)
    order = owner.post(f"/api/funnel-runs/{run['id']}/purchase", json={}).json
    assert pay(app, order).status_code == 200
    drain(app)
    p = privacy(admin)
    app.clock[0] += 24 * 3600
    drain(app)
    assert not any("Тестовое касание 24" in d.get("text", "") for _, d in sent)
    response = owner.post("/api/marketing-consents", json={"policy_id": p["id"], "accepted": True})
    assert response.status_code == 200, response.json
    app.clock[0] += 24 * 3600
    drain(app)
    assert any("Тестовое касание 48" in d.get("text", "") for _, d in sent)


def test_marketing_choice_is_separate_and_revocable(app):
    from marina.db import connect
    from marina.privacy import marketing_allowed

    admin, owner = login(app), login(app, 2)
    p = privacy(admin)
    actor = owner.get("/api/me").json["account"]["id"]
    with connect(app.config["DATABASE"]) as db:
        assert marketing_allowed(db, actor) is False
    for accepted in (True, False):
        response = owner.post(
            "/api/marketing-consents", json={"accepted": accepted, "policy_id": p["id"]}
        )
        assert response.status_code == 200, response.json
        with connect(app.config["DATABASE"]) as db:
            assert marketing_allowed(db, actor) is accepted
    assert owner.get("/api/privacy").json["accepted"] is False


def test_withdrawal_closes_new_data_collection_without_erasing_history(app):
    admin, owner = login(app), login(app, 2)
    p = privacy(admin)
    owner.post(
        "/api/consents",
        json={"policy_id": p["id"], "accepted": True, "representative_confirmed": True},
    )
    app.config["ENFORCE_CONSENT"] = True
    child = {"name": "Fixture", "kind": "child", "birth_date": "2020-01-01"}
    created = owner.post("/api/subjects", json=child)
    assert created.status_code == 201, created.json
    assert owner.post("/api/consents/revoke", json={}).status_code == 200
    assert owner.get("/api/privacy").json["accepted"] is False
    assert owner.post("/api/subjects", json=child).status_code == 422
    assert owner.get("/api/subjects/" + created.json["id"]).status_code == 200
    renewed = owner.post(
        "/api/consents",
        json={"policy_id": p["id"], "accepted": True, "representative_confirmed": True},
    )
    assert renewed.status_code == 200, renewed.json
    assert renewed.json["status"] == "recorded"
    assert owner.get("/api/privacy").json["accepted"] is True
