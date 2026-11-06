import pytest

from tests.test_identity import app
from tests.test_funnel import private_source, setup_sale, scenario, start, finish, drain
from tests.test_commerce import pay


@pytest.mark.parametrize("group", ["3-4", "5-6", "7-9", "10-14"])
@pytest.mark.parametrize("admin_access", [False, True])
@pytest.mark.parametrize("retry_file", [False, True])
def test_tripwire_delivery_uses_published_age_copy_before_pdf(app, group, admin_access, retry_file):
    admin, owner, _, config, _, sent, files = setup_sale(app, group)
    body = f"{{name}}, синтетическое руководство {group} готово.\nСохраните этот тестовый PDF."
    source = "Оплата прошла успешно! 🎉\n" + body
    published = scenario(admin, group, **{**config["data"], "success_text": source})
    actor = admin if admin_access else owner
    run = finish(actor, start(actor, group, key="delivery-copy"), ["b"] * 6)
    # A later publication must not replace the copy promised by this run.
    scenario(admin, group, **{**published["data"], "success_text": "Later synthetic edition"})
    drain(app)
    app.clock[0] += 30
    drain(app)
    app.clock[0] += 180
    drain(app)
    response = actor.post(f"/api/funnel-runs/{run['id']}/purchase", json={})
    assert response.status_code == 200, response.json
    if not admin_access:
        assert pay(app, response.json).status_code == 200

    events = []
    original_transport = app.config["HTTP_TRANSPORT"]

    def transport(method, url, data, headers):
        if url.endswith("/sendMessage") and "синтетическое руководство" in data.get("text", ""):
            events.append(("message", data))
        return original_transport(method, url, data, headers)

    attempts = []

    def send_document(*args):
        events.append(("file", args))
        attempts.append(args)
        if retry_file and len(attempts) == 1:
            raise RuntimeError("Synthetic file upload failure")
        files.append(args)

    app.config["HTTP_TRANSPORT"] = transport
    app.config["FILE_TRANSPORT"] = send_document
    drain(app)
    if retry_file:
        app.clock[0] += 60
        drain(app)

    messages = [data for kind, data in events if kind == "message"]
    expected = body.replace("{name}", "User")
    expected = (
        "Руководство доступно без оплаты.\n\n" + expected
        if admin_access
        else "Оплата прошла успешно! 🎉\n" + expected
    )
    assert len(messages) == 1
    assert messages[0]["text"] == expected
    assert "reply_markup" not in messages[0]
    assert events[0][0] == "message"
    assert len(files) == 1
    assert files[0][1] == published["data"]["guide_filename"]
    assert actor.get(f"/api/funnel-runs/{run['id']}").json["data"]["guide_delivered_at"]
