"""One-shot, fail-closed patch preparation for the isolated audit branch.

This script only edits the listed repository source files. It never opens a live
DB, calls a provider, changes credentials, deploys or pushes. All anchors must
match before any file is written. The cloud validation job supplies --apply.
"""

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS = (
    "marina/funnel.py", "marina/funnel_bot.py", "marina/commerce.py",
    "marina/providers.py", "marina/operations.py", "tests/test_funnel.py",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    source = {name: (ROOT / name).read_text() for name in PATHS}

    def change(name, old, new, count=1):
        actual = source[name].count(old)
        if actual != count:
            raise RuntimeError(f"Anchor mismatch in {name}: expected {count}, found {actual}: {old[:90]!r}")
        source[name] = source[name].replace(old, new)

    f = "marina/funnel.py"
    change(f, "RESULT_WAIT_SECONDS = 30", "RESULT_WAIT_SECONDS = 3")
    change(f, "PROGRESS_STEPS = (0, 20, 40, 60, 80, 100)", "PROGRESS_STEPS = (0,)")
    change(f, "DEFAULT_TRIPWIRE_DELAY_SECONDS = 3 * 60", "DEFAULT_TRIPWIRE_DELAY_SECONDS = 2")
    start = source[f].index("def progress_text(percent):")
    end = source[f].index("\ndef price_text(", start)
    source[f] = source[f][:start] + 'def progress_text(percent):\n    return "Обрабатываю ваши ответы и строю нейропрофиль... ⏳"\n\n' + source[f][end:]
    change(f, '"name": "Нейро-Скрининг | Савельева Марина"', '"name": "Нейро-Скрининг: почему ребенок не слушается и трудно учится"')
    change(f, '"Узнайте причины сложностей с поведением, вниманием и речью вашего ребенка.\\n"', '"Узнайте реальные причины сложностей с поведением, вниманием и речью вашего ребенка.\\n"')
    change(f, "Этот скрининг поможет обратить внимание на сложности с саморегуляцией, утомляемостью и обработкой информации.", "В 9 из 10 случаев дело не в характере и не в лени. Просто определенные отделы мозга\n(лобные доли, мозжечок, ствол) сейчас перегружены или не успевают созревать.\n\nЭтот скрининг покажет, где именно тонкое место у вашего ребенка: в саморегуляции,\nутомляемости мозга или обработке информации.")

    helpers = '''def owned_guide(db, owner, group, admin=False):
    """An active entitlement suppresses repeat sales of this age-family guide."""
    rows = db.execute(
        "SELECT * FROM objects WHERE kind='right' AND owner=? AND status='active' "
        "AND json_extract(data,'$.service')='guide' "
        "AND json_extract(data,'$.product_snapshot.age_group')=? "
        "ORDER BY created DESC,id DESC", (owner, group),
    )
    for row in rows:
        right = unpack(row)
        if right["data"].get("access_source") != "admin" or admin:
            return right
    return None


def result_buttons(obj):
    """Use the same result navigation for the first delivery and resume."""
    data = obj["data"]
    snapshot = data.get("snapshot", {})
    values = {"id": obj["id"]}
    if data.get("owned_guide_id"):
        return [("📄 Открыть руководство", "funnel_owned", {"right_id": data["owned_guide_id"]})]
    zone = data.get("result", {}).get("zone")
    if zone == "green":
        if snapshot.get("free_material_id"):
            return [("🎁 Игры по возрасту — PDF", "funnel_free", values)]
        if snapshot.get("channel_url"):
            return [("📚 Полезные материалы", "url", {"url": snapshot["channel_url"]})]
    if zone == "red":
        return [
            ("📋 Что входит в диагностику", "funnel_consultation", values),
            ("📅 Записаться на диагностику", "funnel_book", values),
        ]
    return []


'''
    change(f, "def public_run(obj):\n", helpers + "def public_run(obj):\n")
    change(f, '    obj = copy.deepcopy(obj)\n    d = obj["data"]\n    snapshot = d.pop("snapshot", {})\n', '    obj = copy.deepcopy(obj)\n    d = obj["data"]\n    if obj["status"] == "completed" and "snapshot" in d:\n        d["result_buttons"] = result_buttons(obj)\n    snapshot = d.pop("snapshot", {})\n')
    change(f, '            # One active questionnaire per account. Old buttons cannot answer a new run.\n', '            snapshot = copy.deepcopy(content["data"])\n            if snapshot.get("free_material_id"):\n                _, free_asset = material_asset(g.db, snapshot["free_material_id"])\n                snapshot["free_asset_id"] = free_asset["id"]\n            # One active questionnaire per account. Old buttons cannot answer a new run.\n')
    change(f, '                    "snapshot": content["data"],\n', '                    "snapshot": snapshot,\n')
    change(f, '        require(obj["status"] == "completed", "Сначала завершите скрининг")\n        ops = policies(g.db, "operations")\n', '        require(obj["status"] == "completed", "Сначала завершите скрининг")\n        prior = owned_guide(g.db, g.actor["id"], obj["data"]["child_age"], admin=admin)\n        if prior:\n            existing = object_get(g.db, prior["data"]["order_id"], "order", g.actor["id"])\n            return {**existing, "existing_guide_right_id": prior["id"]}\n        ops = policies(g.db, "operations")\n')
    change(f, '        _, blob = material_asset(g.db, obj["data"]["snapshot"].get("free_material_id"))\n        return Response(blob["body"], mimetype="application/pdf")\n', '''        snapshot = obj["data"]["snapshot"]
        if snapshot.get("free_asset_id"):
            blob = g.db.execute(
                "SELECT body FROM blobs WHERE id=? AND case_id=? AND mime='application/pdf'",
                (snapshot["free_asset_id"], snapshot["free_material_id"]),
            ).fetchone()
            require(blob, "Файл недоступен", 404)
        else:
            _, blob = material_asset(g.db, snapshot.get("free_material_id"))
        return Response(blob["body"], mimetype="application/pdf")
''')
    change(f, '    require(\n        type(d.get("tripwire_delay_seconds")) is int\n', '''    for followup in followups:
        if "button_label" in followup:
            require(isinstance(followup["button_label"], str) and 1 <= len(followup["button_label"]) <= 120, "Проверьте подпись кнопки касания")
        require(followup.get("button_action", "info") in ("info", "book"), "Проверьте действие касания")
        if "support_label" in followup:
            require(isinstance(followup["support_label"], str) and 1 <= len(followup["support_label"]) <= 120, "Проверьте подпись кнопки связи")
    require(
        type(d.get("tripwire_delay_seconds")) is int
''')
    change(f, '    dev_delivery = False\n    with connect(app.config["DATABASE"]) as db:\n', '    dev_delivery = False\n    prior_guide = None\n    with connect(app.config["DATABASE"]) as db:\n')
    change(f, '        if event in ("funnel.offer", "funnel.cart.first", "funnel.cart.last"):\n            dev_offer = bool(\n', '''        if event in ("funnel.offer", "funnel.cart.first", "funnel.cart.last"):
            prior_guide = owned_guide(db, owner, d.get("child_age"), admin=account["telegram_id"] in app.config["ADMIN_TELEGRAM_IDS"])
            if prior_guide and event.startswith("funnel.cart"):
                return
            dev_offer = bool(
''')
    change(f, '                if order["status"] != "pending":\n                    return\n        if event == "funnel.guide":\n', '                if order["status"] != "pending":\n                    return\n                price = price_text({"data": {"amount": order["data"]["total"], "currency": order["data"]["currency"]}})\n        if event == "funnel.guide":\n')
    change(f, '        require(0 <= step < len(PROGRESS_STEPS), "Некорректный шаг обработки")\n', '        # Old six-step jobs may remain queued during the deployment.\n        if 0 < step <= 5 and len(PROGRESS_STEPS) == 1:\n            return\n        require(0 <= step < len(PROGRESS_STEPS), "Некорректный шаг обработки")\n')
    change(f, '''        buttons = []
        if d["result"]["zone"] == "green" and snapshot.get("free_material_id"):
            buttons.insert(0, ("🎁 Игры по возрасту — PDF", "funnel_free", values))
''', '        buttons = result_buttons(obj)\n')
    change(f, '    elif event == "funnel.offer":\n        expires = d.get("offer_expires", now + 7200)\n', '''    elif event == "funnel.offer":
        if prior_guide:
            bot.send("Руководство для этого возраста уже доступно Вам.", [
                ("📄 Открыть руководство", "funnel_owned", {"right_id": prior_guide["id"]}),
            ])
            with transaction(app.config["DATABASE"]) as db:
                current = object_get(db, id)
                update(db, current, now, {**current["data"], "owned_guide_id": prior_guide["id"]})
                cancel(db, current, ("funnel.cart",))
            return
        expires = d.get("offer_expires", now + 7200)
''')
    change(f, '                ("🆘 Помощь с оплатой", "funnel_consultation", values),\n', '                *([("🆘 Помощь с оплатой", "url", {"url": snapshot["contact_url"]})] if snapshot.get("contact_url") else []),\n')
    change(f, '    elif event == "funnel.guide":\n        if not d.get("guide_file_sent_at"):\n', '''    elif event == "funnel.guide":
        if not d.get("guide_success_sent_at"):
            if dev_delivery:
                message = "Тестовый PDF готов. Оплата не выполнялась."
            elif right["data"].get("access_source") == "admin":
                message = "Руководство доступно без оплаты. PDF будет отправлен следующим сообщением."
            else:
                message = render_copy(snapshot["success_text"], snapshot, account["name"])
            bot.send(message, consult)
            with transaction(app.config["DATABASE"]) as db:
                current = object_get(db, id)
                update(db, current, now, {**current["data"], "guide_success_sent_at": now})
        if not d.get("guide_file_sent_at"):
''')
    change(f, '''        bot.send(
            (
                "Тестовый PDF готов. Оплата не выполнялась."
                if dev_delivery
                else render_copy(snapshot["success_text"], snapshot, account["name"])
            ),
            consult,
        )
''', '')
    change(f, '''        text = next(f["text"] for f in snapshot["followups"] if f["hours"] == hours)
        bot.send(render_copy(text, snapshot, account["name"], deadline=deadline), consult)
''', '''        followup = next(f for f in snapshot["followups"] if f["hours"] == hours)
        copy_values = {
            "deadline": deadline,
            "consultation_price": str(snapshot["consultation_amount_rub"]) + " ₽",
            "consultation_discount": (snapshot["consultation_regular_rub"] - snapshot["consultation_amount_rub"]) * 100 // snapshot["consultation_regular_rub"],
        }
        buttons = [(render_copy(followup.get("button_label", "📋 О диагностике"), snapshot, account["name"], **copy_values),
                    "funnel_book" if followup.get("button_action") == "book" else "funnel_consultation", values)]
        if snapshot.get("contact_url"):
            buttons.append((followup.get("support_label", "💬 Написать Марине"), "url", {"url": snapshot["contact_url"]}))
        bot.send(render_copy(followup["text"], snapshot, account["name"], **copy_values), buttons)
''')

    f = "marina/funnel_bot.py"
    change(f, '            bot.send(result["text"] + "\\n\\n" + result["limitations"])\n', '            bot.send(result["text"] + "\\n\\n" + result["limitations"], run["data"].get("result_buttons", []))\n')
    change(f, '        if order["status"] == "paid":\n            bot.send(\n', '''        if order.get("existing_guide_right_id"):
            bot.send("Доступ открыт. Файл будет доставлен в этот чат." if order["data"].get("access_source") == "admin" else "Руководство уже оплачено. Отправляю ваш файл.")
            return handle_action(bot, "funnel_owned", {"right_id": order["existing_guide_right_id"]})
        if order["status"] == "paid":
            bot.send(
''')
    change(f, '    elif action == "funnel_free":\n', '''    elif action == "funnel_owned":
        from .bot import send_file

        response = bot.client.get("/api/guides/" + d["right_id"] + "/pdf")
        require(response.status_code == 200, (response.json or {}).get("error", "Файл недоступен"))
        send_file(bot.app, bot.chat, "guide.pdf", response.data, "application/pdf")
    elif action == "funnel_free":
''')

    f = "marina/commerce.py"
    change(f, 'def fulfill(db, order, paid_at, now):\n', '''def checkout_open(db, order, now):
    """New invoice eligibility only; received money still goes through confirm."""
    if order["status"] != "pending" or order["data"]["expires"] <= now:
        return False
    run_id = order["data"].get("run_id")
    if not run_id:
        return True
    run = object_get(db, run_id, "funnel_run", order["owner"])
    return bool(run["status"] == "completed" and not run["data"].get("refused_at") and run["data"].get("offer_expires", 0) > now)


def fulfill(db, order, paid_at, now):
''')
    change(f, '''            if row:
                check_lava(app, db, unpack(row), now)
            else:
                insert(
                    db,
                    "reconciliation",
                    {
                        "issue": "unmatched_lava_invoice",
                        "provider_id": p["provider_id"],
                        "payment_event_id": event["id"],
                    },
                    now,
                    status="open",
                )
        update(db, event, now, status="processed")
''', '''            if row:
                check_lava(app, db, unpack(row), now)
                if not db.execute("SELECT 1 FROM receipts WHERE provider='lava' AND charge=?", (p["provider_id"],)).fetchone():
                    return False
            else:
                existing = db.execute("SELECT 1 FROM objects WHERE kind='reconciliation' AND json_extract(data,'$.payment_event_id')=?", (event["id"],)).fetchone()
                if not existing:
                    insert(db, "reconciliation", {"issue": "unmatched_lava_invoice", "provider_id": p["provider_id"], "payment_event_id": event["id"]}, now, status="open")
                return False
        update(db, event, now, status="processed")
''')
    change(f, '                    and o["data"]["expires"] > g.now\n', '                    and checkout_open(db, o, g.now)\n')
    f = "marina/providers.py"
    change(f, "from .commerce import confirm\n", "from .commerce import checkout_open, confirm\n")
    change(f, '            o["status"] == "pending" and o["data"]["expires"] > g.now,\n', '            checkout_open(g.db, o, g.now),\n')
    f = "marina/operations.py"
    change(f, '''        db.execute(
            "UPDATE outbox SET status='processing',attempts=attempts+1,leased_until=? WHERE id=?",
''', '''        if event["event"] == "invoice.create" and event["status"] == "pending":
            from .commerce import checkout_open

            attempt = object_get(db, event["object_id"], "payment_attempt")
            order = object_get(db, attempt["data"]["order_id"], "order")
            if attempt["status"] == "queued" and not checkout_open(db, order, now):
                update(db, attempt, now, status="cancelled")
                db.execute("UPDATE outbox SET status='cancelled',leased_until=NULL WHERE id=?", (event["id"],))
                return 1
        db.execute(
            "UPDATE outbox SET status='processing',attempts=attempts+1,leased_until=? WHERE id=?",
''')
    change(f, '            process_payment_event(app, event["object_id"], now)\n', '            require(process_payment_event(app, event["object_id"], now) is not False, "Подтверждение платежа ещё не получено", 503)\n')
    change(f, '                "sent" if outcome == "sent" else "failed" if current["attempts"] >= 8 else "pending"\n', '                "sent" if outcome == "sent" else "failed" if current["attempts"] >= 8 and event["event"] != "payment.process" else "pending"\n')
    change(f, '2 ** current["attempts"] * 10', '2 ** min(current["attempts"], 9) * 10')

    f = "tests/test_funnel.py"
    start = source[f].index("def test_result_waits_30_seconds_and_edits_one_progress_message(app):")
    end = source[f].index('@pytest.mark.parametrize("group"', start)
    source[f] = source[f][:start] + '''def test_result_waits_three_seconds_and_edits_one_progress_message(app):
    # The supplied owner document replaces the former 30-second preview contract.
    admin, owner = login(app), login(app, 2)
    scenario(admin)
    calls = []
    def transport(method, url, data, headers):
        calls.append((url.rsplit("/", 1)[-1], data))
        return {"ok": True, "result": {"message_id": 77}}
    app.config["HTTP_TRANSPORT"] = transport
    run = finish(owner, start(owner), ["b"] * 6)
    with connect(app.config["DATABASE"]) as db:
        rows = db.execute("SELECT event,available FROM outbox WHERE object_id=? ORDER BY available,event", (run["id"],)).fetchall()
    assert [(r["event"], r["available"] - run["data"]["completed_at"]) for r in rows] == [("funnel.progress.0", 0), ("funnel.result", 3)]
    drain(app)
    assert len(calls) == 1 and calls[0][0] == "sendMessage"
    app.clock[0] += 2
    drain(app)
    assert len(calls) == 1
    app.clock[0] += 1
    drain(app)
    assert calls[-1][0] == "editMessageText" and calls[-1][1]["message_id"] == 77
    assert "ИТОГ СКРИНИНГА" in calls[-1][1]["text"]
    assert "progress_message_id" not in owner.get("/api/funnel-runs/" + run["id"]).json["data"]


''' + source[f][end:]
    change(f, 'raise RuntimeError("Synthetic sendMessage failure after sendDocument")', 'raise RuntimeError("Synthetic confirmation failure before sendDocument")')
    change(f, '    app.config["HTTP_TRANSPORT"] = failing_success\n    drain(app)\n    assert len(files) == 1\n    app.clock[0] += 60\n', '    app.config["HTTP_TRANSPORT"] = failing_success\n    drain(app)\n    assert len(files) == 0  # Confirmation precedes the file, per the source document.\n    app.clock[0] += 60\n')
    # Only the historical red-result test is changed; it previously asserted no CTA.
    start = source[f].index("def test_result_buttons_match_green_and_red_paths(app):")
    end = source[f].index("\ndef test_refusal_can_deliver", start)
    block = source[f][start:end]
    block = block.replace('    assert "reply_markup" not in sent[-1][1]\n', '    assert [b["text"] for row in sent[-1][1]["reply_markup"]["inline_keyboard"] for b in row] == ["📋 Что входит в диагностику", "📅 Записаться на диагностику"]\n')
    source[f] = source[f][:start] + block + source[f][end:]
    for name, content in source.items():
        ast.parse(content, filename=name)
    for name, content in source.items():
        (ROOT / name).write_text(content)
        print("Prepared", name)


if __name__ == "__main__":
    main()
