"""Owner-supplied six-question funnel, versioned copy and durable follow-ups."""

import copy
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Response, current_app, g

from . import body, object_get, protected, reason, require
from .db import audit, connect, emit, insert, transaction, uid, unpack, update
from .screening import idempotent, score, validate_method

AGES = {"3-4": "🧒 3–4 года", "5-6": "👦 5–6 лет", "7-9": "🎒 7–9 лет", "10-14": "🧑 10–14 лет"}
AGE_MONTHS = {"3-4": (36, 59), "5-6": (60, 83), "7-9": (84, 119), "10-14": (120, 179)}
WELCOME = """Здравствуйте, {name}!
Меня зовут Савельева Марина.
Я практикующий психолог, нейропсихолог и дефектолог с опытом работы более 18 лет.

Ко мне часто приводят детей со словами:
❌ «Он просто ленится»
❌ «Она делает назло и не слушает с первого раза»
❌ «Учителя жалуются, что он витает в облаках»

Этот скрининг поможет обратить внимание на сложности с саморегуляцией, утомляемостью и обработкой информации.

⏳ Время прохождения: 3 минуты (всего 6 вопросов).
🔒 Тест имеет ориентировочный характер, не является медицинским диагнозом, но подсветит зоны риска, требующие внимания.

Выберите возраст ребёнка, чтобы вопросы были точными ⬇️"""
AGE_REMINDER = """{name}, отвлеклись? Понимаю, с детьми сложно найти свободную минуту 😊
Тест займет всего 3 минуты, а результаты помогут сберечь километры ваших нервов при выполнении уроков и домашних дел.
Какого возраста ваш ребенок? Жмите кнопку ниже ⬇️"""
PROFILE = {
    "name": "Нейро-Скрининг | Савельева Марина",
    "description": "Пройдите экспресс-тест от нейропсихолога и дефектолога за 3 минуты.\n"
    "Узнайте причины сложностей с поведением, вниманием и речью вашего ребенка.\n"
    "Нажмите кнопку «СТАРТ» внизу ⬇️",
}
RESULT_WAIT_SECONDS = 30
PROGRESS_STEPS = (0, 20, 40, 60, 80, 100)


def progress_text(percent):
    filled = percent // 10
    return (
        "Обрабатываю ваши ответы и готовлю результат…\n\n"
        + "🟩" * filled
        + "⬜" * (10 - filled)
        + f" {percent}%"
    )


def price_text(product):
    amount = product["data"]["amount"]
    currency = product["data"]["currency"]
    if currency == "XTR":
        return str(amount) + " ⭐"
    require(currency == "RUB", "Валюта тарифа не поддерживается")
    return f"{amount // 100},{amount % 100:02d} ₽"


def lava_guide_ready(app, product):
    data = product["data"]
    return bool(
        app.config.get("DEV_EXTERNAL_LAVA_CHECKOUT", False)
        and data.get("currency") == "RUB"
        and data.get("web_sales_approved") is True
        and data.get("lava_offer_id")
        and app.config.get("LAVA_API_KEY")
        and app.config.get("LAVA_WEBHOOK_SECRET")
        and str(app.config.get("PUBLIC_URL", "")).startswith("https://")
    )


def defaults(group):
    require(group in AGES, "Выберите возраст: 3–4, 5–6, 7–9 или 10–14 лет")
    try:
        data = json.loads(Path(current_app.config["FUNNEL_SOURCE_PATH"]).read_text())
        require(
            isinstance(data, dict) and isinstance(data.get(group), dict), "Проверьте файл сценария"
        )
    except (OSError, ValueError):
        require(False, "Файл сценария ещё не загружен в закрытое хранилище")
    return {**data[group], "welcome_text": WELCOME, "age_reminder_text": AGE_REMINDER}


def published(db, group=None):
    row = db.execute(
        "SELECT * FROM objects WHERE kind='funnel' AND status='published' "
        "AND (? IS NULL OR json_extract(data,'$.age_group')=?) "
        "ORDER BY updated DESC,rowid DESC LIMIT 1",
        (group, group),
    ).fetchone()
    return unpack(row)


def validate_funnel(d):
    require(
        isinstance(d.get("age_group"), str) and d["age_group"] in AGES,
        "Выберите возрастную группу скрининга",
    )
    m = d.get("method")
    require(isinstance(m, dict), "Добавьте методику скрининга")
    validate_method(m)
    require(
        m["audience"] == "child"
        and (m["min_months"], m["max_months"]) == AGE_MONTHS[d["age_group"]],
        "Возраст методики должен совпадать с веткой",
    )
    require(
        len(m["questions"]) == 6
        and all(not q.get("when") and q.get("required", True) for q in m["questions"])
        and all([o["score"] for o in q["options"]] == [0, 1, 2] for q in m["questions"]),
        "В скрининге нужны 6 обязательных вопросов с вариантами на 0, 1 и 2 балла",
    )
    require(
        [(b["min"], b["max"], b.get("zone")) for b in m["bands"]]
        == [(0, 3, "green"), (4, 7, "yellow"), (8, 12, "red")],
        "Диапазоны: 0–3 зелёный, 4–7 жёлтый, 8–12 красный",
    )
    for key in (
        "welcome_text",
        "age_reminder_text",
        "offer_text",
        "refusal_text",
        "consultation_text",
        "success_text",
        "cart_first_text",
        "cart_last_text",
        "guide_title",
        "buyer_tag",
        "refused_tag",
        "consultation_intro_text",
    ):
        require(
            isinstance(d.get(key), str) and 1 <= len(d[key]) <= 16000, "Проверьте текст: " + key
        )
    for key in ("channel_url", "contact_url"):
        url = d.get(key, "")
        require(
            isinstance(url, str)
            and (not url or (urlparse(url).scheme == "https" and urlparse(url).hostname == "t.me")),
            "Укажите HTTPS-ссылку t.me для " + key,
        )
    try:
        ZoneInfo(d.get("timezone", ""))
    except (ValueError, ZoneInfoNotFoundError):
        require(False, "Укажите часовой пояс IANA")
    followups = d.get("followups")
    require(
        isinstance(followups, list)
        and all(isinstance(f, dict) for f in followups)
        and [f.get("hours") for f in followups] == [24, 48, 72],
        "Нужны касания через 24, 48 и 72 часа",
    )
    require(
        all(isinstance(f.get("text"), str) and 1 <= len(f["text"]) <= 16000 for f in followups),
        "Проверьте тексты касаний",
    )
    pages = d.get("guide_pages")
    require(
        isinstance(pages, list)
        and len(pages) == 14
        and all(
            isinstance(p, dict)
            and isinstance(p.get("title"), str)
            and p["title"].strip()
            and isinstance(p.get("text"), str)
            and 1 <= len(p["text"]) <= 20000
            for p in pages
        ),
        "Нужны 14 страниц руководства",
    )
    require(
        isinstance(d.get("guide_filename"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]+\.pdf", d["guide_filename"]),
        "Укажите имя PDF латиницей",
    )
    for key in ("guide_product_id", "free_material_id", "refusal_material_id"):
        require(isinstance(d.get(key, ""), str), "Проверьте ссылку на материал или продукт")
    require(
        type(d.get("consultation_valid_days")) is int and 1 <= d["consultation_valid_days"] <= 365,
        "Укажите срок промокода в днях",
    )
    for key in ("consultation_amount_rub", "consultation_regular_rub"):
        require(type(d.get(key)) is int and d[key] > 0, "Укажите цену консультации из документа")
    require(
        isinstance(d.get("consultation_code"), str) and d["consultation_code"].strip(),
        "Укажите промокод из документа",
    )
    if d.get("guide_product_id"):
        require(d.get("contact_url"), "Укажите контакт специалиста для поддержки покупателей")
        p = object_get(g.db, d["guide_product_id"], "product")
        require(
            p["status"] == "published"
            and p["data"].get("service") == "guide"
            and p["data"].get("age_group") == d["age_group"],
            "Нужен опубликованный продукт-руководство того же возраста",
        )
    if d.get("free_material_id"):
        material_asset(g.db, d["free_material_id"])
    if d.get("refusal_material_id"):
        material_asset(g.db, d["refusal_material_id"], ("application/pdf", "video/mp4"))


def material_asset(db, material_id, mimes=("application/pdf",)):
    obj = object_get(db, material_id, "material")
    require(obj["status"] == "published", "Материал ещё не опубликован")
    row = db.execute(
        "SELECT * FROM blobs WHERE id=? AND case_id=? AND mime IN ("
        + ",".join("?" for _ in mimes)
        + ")",
        (obj["data"].get("asset_id"), material_id, *mimes),
    ).fetchone()
    require(row, "Прикрепите PDF к опубликованному материалу")
    return obj, row


def public_run(obj):
    obj = copy.deepcopy(obj)
    d = obj["data"]
    snapshot = d.pop("snapshot", {})
    d.pop("approval", None)
    d.pop("progress_message_id", None)
    d.pop("progress_percent", None)
    if obj["status"] == "in_progress" and snapshot:
        index = len(d["answers"])
        q = copy.deepcopy(snapshot["method"]["questions"][index])
        for option in q["options"]:
            option.pop("score", None)
        d["question"] = q
    return obj


def schedule(db, obj, event, at):
    # Object/event is unique in this workflow. Transitions and scheduling share a transaction.
    if not db.execute(
        "SELECT 1 FROM outbox WHERE object_id=? AND event=?", (obj["id"], event)
    ).fetchone():
        emit(db, obj["owner"], obj["id"], event, at)


def cancel(db, obj, prefixes):
    for prefix in prefixes:
        db.execute(
            "UPDATE outbox SET status='cancelled',leased_until=NULL WHERE object_id=? AND event LIKE ? AND status IN ('pending','processing')",
            (obj["id"], prefix + "%"),
        )


def render_copy(text, snapshot, name, **values):
    replacements = {
        "name": name or "Здравствуйте",
        "channel_url": snapshot.get("channel_url", ""),
        "contact_url": snapshot.get("contact_url", ""),
        **values,
    }
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", str(value))
    # Do not expose source-document link placeholders as working destinations.
    return re.sub(r"\[(?:Ссылка[^\]]*|@ваш_юзернейм)\]", "", text).strip()


def stop_for_owner(db, owner, now):
    rows = db.execute(
        "SELECT * FROM objects WHERE owner=? AND kind IN ('funnel_entry','funnel_run')", (owner,)
    ).fetchall()
    for row in rows:
        obj = unpack(row)
        cancel(
            db,
            obj,
            ("funnel.age", "funnel.offer", "funnel.cart", "funnel.followup", "funnel.refusal"),
        )
        update(db, obj, now, {**obj["data"], "notifications_stopped": True})


def guide_paid(db, order, right, now):
    run = object_get(db, order["data"]["run_id"], "funnel_run", order["owner"])
    run = update(
        db,
        run,
        now,
        {
            **run["data"],
            "order_status": "paid",
            "guide_right_id": right["id"],
            "tags": sorted(
                set(run["data"].get("tags", []) + [run["data"]["snapshot"]["buyer_tag"]])
            ),
        },
    )
    cancel(db, run, ("funnel.offer", "funnel.cart", "funnel.refusal"))
    schedule(db, run, "funnel.guide", now)


def guide_quote(db, actor, p, d, now, allow_external_lava=False):
    """Use the existing quote/order/receipt pipeline for an age-only guide purchase."""
    from .commerce import policies

    run = object_get(db, d.get("run_id"), "funnel_run", actor)
    r = run["data"]
    require(
        run["status"] == "completed"
        and r.get("offer_expires", 0) > now
        and not r.get("refused_at"),
        "Предложение истекло или закрыто",
    )
    require(not r.get("guide_right_id"), "Руководство уже оплачено", 409)
    require(
        p["id"] == r["snapshot"].get("guide_product_id")
        and p["status"] == "published"
        and p["data"].get("service") == "guide"
        and p["data"].get("age_group") == r["child_age"],
        "Руководство не соответствует скринингу",
    )
    surface = d.get("surface")
    require(surface in ("telegram", "web"), "Укажите поверхность покупки")
    external_lava = (
        surface == "telegram"
        and p["data"]["currency"] == "RUB"
        and allow_external_lava
        and p["data"].get("web_sales_approved") is True
    )
    require(
        surface != "telegram" or p["data"]["currency"] == "XTR" or external_lava,
        "Внешняя оплата цифрового материала для этой среды не разрешена",
    )
    require(
        surface != "web" or p["data"].get("web_sales_approved") is True,
        "Веб-продажи пока не согласованы",
    )
    ops = policies(db, "operations")
    require(not ops or not ops["data"].get("pause_sales"), "Приём новых заказов приостановлен")
    policy = policies(db, "commerce", "guide")
    require(policy, "Сначала утвердите правила покупки в админке")
    material, asset = material_asset(db, p["data"].get("material_id"))
    intent = idempotent(
        db,
        "guide-intent:" + actor,
        run["id"],
        {},
        lambda: insert(
            db,
            "purchase_intent",
            {"service": "guide", "subject_ids": [run["id"]], "reference": run["id"]},
            now,
            actor,
            status="open",
        ),
    )
    require(
        not db.execute(
            "SELECT 1 FROM intent_fulfillments WHERE intent_id=?", (intent["id"],)
        ).fetchone(),
        "Покупка уже исполнена",
        409,
    )
    amount = p["data"]["amount"]
    payload = {
        "intent_id": intent["id"],
        "product_id": p["id"],
        "product_snapshot": p["data"],
        "policy_id": policy["id"],
        "policy_snapshot": policy["data"],
        "family": [],
        "run_id": run["id"],
        "material_id": material["id"],
        "asset_id": asset["id"],
        "lines": [
            {"id": uid(), "subject_id": run["id"], "base": amount, "amount": amount, "discount": 0}
        ],
        "total": amount,
        "currency": p["data"]["currency"],
        "provider": "stars" if p["data"]["currency"] == "XTR" else "lava",
        "surface": surface,
        "expires": min(r["offer_expires"], now + policy["data"]["quote_ttl_seconds"]),
    }
    quote = insert(db, "quote", payload, now, actor, status="valid")
    audit(db, actor, "quote.create", quote, now, "Расчёт покупки возрастного руководства")
    return quote


def render_guide(d):
    """Review PDF from the supplied pages; no new methodological text is generated."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    if "Marina" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont("Marina", str(Path(__file__).parent / "assets/DejaVuSans.ttf"))
        )
    target = io.BytesIO()
    style = ParagraphStyle("guide", fontName="Marina", fontSize=10, leading=14)
    heading = ParagraphStyle("heading", parent=style, fontSize=15, leading=20, spaceAfter=16)
    story = []
    for i, page in enumerate(d["guide_pages"]):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(page["title"]), heading))
        text = render_copy(page["text"], d, "")
        for line in text.splitlines():
            story.extend((Paragraph(escape(line) or " ", style), Spacer(1, 4)))
    SimpleDocTemplate(target, title=d["guide_title"], author="Савельева Марина").build(story)
    return target.getvalue()


def render_text_material(d):
    """Render administrator-supplied copy without inventing product content."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    if "Marina" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont("Marina", str(Path(__file__).parent / "assets/DejaVuSans.ttf"))
        )
    target = io.BytesIO()
    style = ParagraphStyle("material", fontName="Marina", fontSize=11, leading=16)
    heading = ParagraphStyle("material-heading", parent=style, fontSize=17, leading=22)
    story = [Paragraph(escape(d["title"]), heading), Spacer(1, 16)]
    for line in d["text"].splitlines() or [""]:
        story.extend((Paragraph(escape(line) or " ", style), Spacer(1, 6)))
    SimpleDocTemplate(target, title=d["title"], author="Савельева Марина").build(story)
    return target.getvalue()


def register_funnel(app):
    @app.get("/api/admin/funnel-defaults/<group>")
    @protected(admin=True)
    def funnel_defaults(group):
        return defaults(group)

    @app.get("/api/funnel/welcome")
    @protected()
    def welcome():
        obj = published(g.db)
        return {"text": obj["data"]["welcome_text"] if obj else WELCOME, "ages": AGES}

    @app.post("/api/funnel-entries")
    @protected()
    def enter():
        d = body()

        def create():
            for row in g.db.execute(
                "SELECT * FROM objects WHERE kind='funnel_entry' AND owner=? AND status='waiting_age'",
                (g.actor["id"],),
            ).fetchall():
                old = unpack(row)
                cancel(g.db, old, ("funnel.age",))
                update(g.db, old, g.now, status="superseded")
            obj = insert(g.db, "funnel_entry", {}, g.now, g.actor["id"], status="waiting_age")
            schedule(g.db, obj, "funnel.age", g.now + 20 * 60)
            return obj

        return idempotent(g.db, "funnel-entry:" + g.actor["id"], d.get("key"), {}, create)

    @app.post("/api/funnel-entries/<id>/age")
    @protected()
    def select_age(id):
        obj = object_get(g.db, id, "funnel_entry", g.actor["id"])
        group = body().get("age_group")
        require(group in (*AGES, "outside"), "Выберите возраст")
        require(obj["status"] in ("waiting_age", "age_selected"), "Начните заново: /start", 409)
        cancel(g.db, obj, ("funnel.age",))
        return update(g.db, obj, g.now, {"child_age": group}, "age_selected")

    @app.post("/api/funnel-runs")
    @protected()
    def begin():
        d = body()
        group = d.get("age_group")
        require(isinstance(group, str) and group in AGES, "Выберите возраст ребёнка")

        def create():
            content = published(g.db, group)
            require(
                content, "Скрининг для этого возраста готовится. Можно обратиться к специалисту."
            )
            # One active questionnaire per account. Old buttons cannot answer a new run.
            for row in g.db.execute(
                "SELECT * FROM objects WHERE kind='funnel_run' AND owner=? AND status='in_progress'",
                (g.actor["id"],),
            ).fetchall():
                old = unpack(row)
                update(g.db, old, g.now, status="superseded")
            obj = insert(
                g.db,
                "funnel_run",
                {
                    "child_age": group,
                    "content_id": content["id"],
                    "snapshot": content["data"],
                    "answers": {},
                    "tags": ["status:screening_started"],
                    "order_status": None,
                },
                g.now,
                g.actor["id"],
                status="in_progress",
            )
            audit(g.db, g.actor["id"], "funnel.start", obj, g.now, "Начало возрастного скрининга")
            return obj

        return public_run(
            idempotent(
                g.db, "funnel-run:" + g.actor["id"], d.get("key"), {"age_group": group}, create
            )
        )

    @app.get("/api/funnel-runs/<id>")
    @protected()
    def read(id):
        return public_run(object_get(g.db, id, "funnel_run", g.actor["id"]))

    @app.post("/api/funnel-runs/<id>/resume")
    @protected()
    def resume(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(
            obj["status"] in ("in_progress", "completed"), "Начните скрининг заново: /start", 409
        )
        for row in g.db.execute(
            "SELECT * FROM objects WHERE kind='funnel_entry' AND owner=? AND status='waiting_age'",
            (g.actor["id"],),
        ).fetchall():
            entry = unpack(row)
            cancel(g.db, entry, ("funnel.age",))
            update(
                g.db,
                entry,
                g.now,
                {**entry["data"], "child_age": obj["data"]["child_age"]},
                "age_selected",
            )
        return public_run(obj)

    @app.post("/api/funnel-runs/<id>/answer")
    @protected()
    def answer(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        d, data = body(), obj["data"]
        answers = dict(data["answers"])
        qid, value = d.get("question"), d.get("value")
        require(isinstance(qid, str) and isinstance(value, str), "Выберите вопрос и вариант ответа")
        if (
            obj["status"] in ("in_progress", "completed")
            and answers.get(qid) == value
            and qid in answers
        ):
            return public_run(obj)
        require(
            obj["status"] == "in_progress" and obj["revision"] == d.get("revision"),
            "Ответ уже сохранён или скрининг завершён",
            409,
        )
        q = data["snapshot"]["method"]["questions"][len(answers)]
        require(
            qid == q["id"] and isinstance(value, str) and value in {o["id"] for o in q["options"]},
            "Выберите ответ на текущий вопрос",
        )
        answers[qid] = value
        payload = {**data, "answers": answers}
        status = "in_progress"
        if len(answers) == 6:
            m = data["snapshot"]["method"]
            result = score(m, answers)
            band = next(b for b in m["bands"] if b["min"] <= result["score"] <= b["max"])
            result.update(
                zone=band["zone"],
                text=render_copy(
                    result["text"], data["snapshot"], g.actor["name"], score=result["score"]
                ),
            )
            payload.update(result=result, completed_at=g.now)
            payload["tags"] = ["status:screening_completed"]
            status = "completed"
        obj = update(g.db, obj, g.now, payload, status)
        audit(
            g.db,
            g.actor["id"],
            "funnel.answer",
            obj,
            g.now,
            "Ответ на вопрос скрининга",
            obj["revision"] - 1,
        )
        if status == "completed":
            for step in range(len(PROGRESS_STEPS)):
                schedule(g.db, obj, f"funnel.progress.{step}", g.now + step * 5)
            schedule(g.db, obj, "funnel.result", g.now + RESULT_WAIT_SECONDS)
        return public_run(obj)

    @app.post("/api/funnel-runs/<id>/refuse")
    @protected()
    def refuse(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(obj["status"] == "completed", "Сначала завершите скрининг")
        require(obj["data"].get("order_status") != "paid", "Руководство уже оплачено")
        require(
            not obj["data"].get("dev_tripwire_accepted_at"),
            "Тестовое руководство уже принято",
        )
        if obj["data"].get("refused_at"):
            return public_run(obj)
        cancel(g.db, obj, ("funnel.offer", "funnel.cart"))
        snapshot = obj["data"]["snapshot"]
        result = update(
            g.db,
            obj,
            g.now,
            {
                **obj["data"],
                "refused_at": g.now,
                "tags": sorted(set(obj["data"]["tags"] + [snapshot["refused_tag"]])),
            },
        )
        if snapshot.get("refusal_material_id"):
            schedule(
                g.db,
                result,
                "funnel.refusal",
                g.now + (4 if snapshot["age_group"] == "3-4" else 5) * 86400,
            )
        audit(
            g.db,
            g.actor["id"],
            "funnel.refuse",
            result,
            g.now,
            "Отказ от предложения руководства",
            obj["revision"],
        )
        return {
            **public_run(result),
            "message": render_copy(snapshot["refusal_text"], snapshot, g.actor["name"]),
        }

    @app.post("/api/funnel-runs/<id>/book")
    @protected()
    def book(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(obj["status"] == "completed", "Сначала завершите скрининг")
        if obj["data"].get("booking_id"):
            return object_get(g.db, obj["data"]["booking_id"], "booking", g.actor["id"])
        preferences = body().get("preferences", "")
        require(
            isinstance(preferences, str) and 1 <= len(preferences.strip()) <= 2000,
            "Напишите удобное время и часовой пояс",
        )
        booking = insert(
            g.db,
            "booking",
            {
                "run_id": id,
                "topic": "Диагностика: " + AGES[obj["data"]["child_age"]],
                "preferences": preferences.strip(),
                "proposals": [],
                "current_proposal": None,
                "promotion": {
                    "code": obj["data"]["snapshot"]["consultation_code"],
                    "amount_rub": obj["data"]["snapshot"]["consultation_amount_rub"],
                    "regular_amount_rub": obj["data"]["snapshot"]["consultation_regular_rub"],
                    "eligible": bool(
                        obj["data"].get("guide_delivered_at")
                        and not obj["data"].get("dev_tripwire_accepted_at")
                        and obj["data"].get("consultation_expires", 0) > g.now
                    ),
                    "expires": obj["data"].get("consultation_expires"),
                },
            },
            g.now,
            g.actor["id"],
            status="requested",
        )
        cancel(g.db, obj, ("funnel.offer", "funnel.cart", "funnel.followup", "funnel.refusal"))
        update(g.db, obj, g.now, {**obj["data"], "booking_id": booking["id"]})
        emit(g.db, booking["owner"], booking["id"], "booking.requested", g.now)
        audit(
            g.db,
            g.actor["id"],
            "funnel.booking",
            booking,
            g.now,
            "Заявка на диагностику после скрининга",
        )
        return booking

    @app.post("/api/funnel/stop")
    @protected()
    def stop():
        stop_for_owner(g.db, g.actor["id"], g.now)
        return {"stopped": True}

    @app.get("/api/funnel-runs/<id>/consultation")
    @protected()
    def consultation(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(obj["status"] == "completed", "Сначала завершите скрининг")
        data = obj["data"]
        eligible = (
            data.get("guide_delivered_at")
            and not data.get("dev_tripwire_accepted_at")
            and data.get("consultation_expires", 0) > g.now
        )
        key = "consultation_text" if eligible else "consultation_intro_text"
        return {"text": render_copy(data["snapshot"][key], data["snapshot"], g.actor["name"])}

    @app.post("/api/funnel-runs/<id>/dev-tripwire-accept")
    @protected()
    def accept_dev_tripwire(id):
        require(
            app.config.get("DEV_TRIPWIRE_BYPASS_PAYMENT", False),
            "Тестовый проход без оплаты отключён",
            404,
        )
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        data = obj["data"]
        require(
            obj["status"] == "completed" and data.get("result", {}).get("zone") == "yellow",
            "Тестовый трипваер недоступен для этого результата",
        )
        require(
            not data.get("snapshot", {}).get("guide_product_id"),
            "Для опубликованного продукта требуется оплата",
        )
        if data.get("dev_tripwire_accepted_at"):
            return public_run(obj)
        require(
            data.get("offer_expires", 0) > g.now
            and not data.get("refused_at")
            and not data.get("booking_id"),
            "Предложение истекло или закрыто",
        )
        result = update(
            g.db,
            obj,
            g.now,
            {
                **data,
                "dev_tripwire_accepted_at": g.now,
                "tags": sorted(set(data.get("tags", []) + ["dev:tripwire_accepted"])),
            },
        )
        cancel(g.db, result, ("funnel.offer", "funnel.cart", "funnel.refusal"))
        schedule(g.db, result, "funnel.guide", g.now)
        audit(
            g.db,
            g.actor["id"],
            "funnel.dev_tripwire_accept",
            result,
            g.now,
            "Ручной dev-проход без платежа",
            obj["revision"],
        )
        return public_run(result)

    @app.post("/api/funnel-runs/<id>/purchase")
    @protected()
    def purchase(id):
        from .commerce import policies

        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(obj["status"] == "completed", "Сначала завершите скрининг")
        ops = policies(g.db, "operations")
        require(not ops or not ops["data"].get("pause_sales"), "Приём новых заказов приостановлен")
        require(
            not obj["data"].get("refused_at") and obj["data"].get("offer_expires", 0) > g.now,
            "Предложение истекло или закрыто",
        )
        if obj["data"].get("order_id"):
            order = object_get(g.db, obj["data"]["order_id"], "order", g.actor["id"])
            if order["status"] == "paid" or (
                order["status"] == "pending" and order["data"]["expires"] > g.now
            ):
                return order
        product = object_get(g.db, obj["data"]["snapshot"].get("guide_product_id"), "product")
        quote = guide_quote(
            g.db,
            g.actor["id"],
            product,
            {"run_id": id, "surface": "telegram"},
            g.now,
            allow_external_lava=lava_guide_ready(app, product),
        )
        order = insert(
            g.db,
            "order",
            {**quote["data"], "quote_id": quote["id"]},
            g.now,
            g.actor["id"],
            status="pending",
        )
        first = obj["data"].get("checkout_started_at", g.now)
        run = update(
            g.db,
            obj,
            g.now,
            {
                **obj["data"],
                "order_id": order["id"],
                "order_status": "pending",
                "checkout_started_at": first,
            },
        )
        if first + 1800 < run["data"]["offer_expires"] - 900:
            schedule(g.db, run, "funnel.cart.first", first + 1800)
        if run["data"]["offer_expires"] - 900 > g.now:
            schedule(g.db, run, "funnel.cart.last", run["data"]["offer_expires"] - 900)
        audit(g.db, g.actor["id"], "order.create", order, g.now, "Заказ возрастного руководства")
        return order

    @app.get("/api/funnel-runs/<id>/free-pdf")
    @protected()
    def free_pdf(id):
        obj = object_get(g.db, id, "funnel_run", g.actor["id"])
        require(
            obj["status"] == "completed" and obj["data"]["result"]["zone"] == "green",
            "Материал доступен после результата зелёной зоны",
            403,
        )
        _, blob = material_asset(g.db, obj["data"]["snapshot"].get("free_material_id"))
        return Response(blob["body"], mimetype="application/pdf")

    @app.get("/api/guides/<id>/pdf")
    @protected()
    def guide_pdf(id):
        right = object_get(g.db, id, "right", g.actor["id"])
        require(
            right["status"] == "active" and right["data"].get("service") == "guide",
            "Нет доступа к руководству",
            403,
        )
        row = g.db.execute(
            "SELECT * FROM blobs WHERE id=?", (right["data"]["asset_id"],)
        ).fetchone()
        require(row, "Файл недоступен", 404)
        return Response(
            row["body"],
            mimetype="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="guide.pdf"'},
        )

    @app.post("/api/admin/funnels/<id>/prepare-guide")
    @protected(admin=True)
    def prepare_guide(id):
        obj = object_get(g.db, id, "funnel")
        d = body()
        why = reason(d)
        require(obj["revision"] == d.get("revision"), "Сценарий изменён", 409)
        validate_funnel(obj["data"])
        pdf = render_guide(obj["data"])
        material = insert(
            g.db,
            "material",
            {
                "title": obj["data"]["guide_title"],
                "owner": "Специалист",
                "status": "received",
                "source": obj["data"]["method"]["source"],
                "rights": obj["data"]["method"]["rights"],
                "format": "pdf",
                "funnel_id": id,
            },
            g.now,
        )
        asset_id = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                asset_id,
                g.actor["id"],
                "",
                material["id"],
                "application/pdf",
                hashlib.sha256(pdf).hexdigest(),
                pdf,
                g.now,
            ),
        )
        material = update(g.db, material, g.now, {**material["data"], "asset_id": asset_id})
        audit(g.db, g.actor["id"], "funnel.prepare_guide", material, g.now, why)
        return material, 201

    @app.get("/api/admin/funnels/<id>/guide-preview")
    @protected(admin=True)
    def guide_preview(id):
        obj = object_get(g.db, id, "funnel")
        validate_funnel(obj["data"])
        return Response(render_guide(obj["data"]), mimetype="application/pdf")

    @app.get("/api/admin/materials/<id>/pdf")
    @protected(admin=True)
    def material_preview(id):
        obj = object_get(g.db, id, "material")
        row = g.db.execute(
            "SELECT * FROM blobs WHERE id=? AND case_id=? AND mime='application/pdf'",
            (obj["data"].get("asset_id"), id),
        ).fetchone()
        require(row, "PDF не прикреплён", 404)
        return Response(row["body"], mimetype="application/pdf")

    @app.post("/api/admin/materials/<id>/render-pdf")
    @protected(admin=True)
    def render_material_pdf(id):
        obj = object_get(g.db, id, "material")
        d = body()
        why = reason(d)
        require(obj["status"] == "draft", "PDF формируется только для черновика")
        require(obj["revision"] == d.get("revision"), "Материал изменён", 409)
        text = obj["data"].get("text")
        filename = obj["data"].get("filename")
        require(isinstance(text, str) and 1 <= len(text) <= 50000, "Введите текст PDF")
        require(
            isinstance(filename, str) and re.fullmatch(r"[A-Za-z0-9_-]+\.pdf", filename),
            "Укажите имя PDF латиницей",
        )
        pdf = render_text_material(obj["data"])
        from .content import material_source_digest

        asset_id = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                asset_id,
                g.actor["id"],
                "",
                obj["id"],
                "application/pdf",
                hashlib.sha256(pdf).hexdigest(),
                pdf,
                g.now,
            ),
        )
        result = update(
            g.db,
            obj,
            g.now,
            {
                **obj["data"],
                "asset_id": asset_id,
                "format": "pdf",
                "render_source_sha256": material_source_digest(obj["data"]),
            },
        )
        audit(
            g.db,
            g.actor["id"],
            "material.render_pdf",
            result,
            g.now,
            why,
            obj["revision"],
        )
        return result

    @app.get("/api/admin/tripwire")
    @protected(admin=True)
    def tripwire_admin():
        from .commerce import policies

        entity_kinds = (
            "funnel",
            "material",
            "product",
            "policy",
            "funnel_run",
            "purchase_intent",
            "quote",
            "order",
            "payment_attempt",
            "payment_event",
            "right",
            "reconciliation",
        )
        entities = {kind: {} for kind in entity_kinds}
        for row in g.db.execute(
            "SELECT kind,status,count(*) AS count FROM objects WHERE kind IN ("
            + ",".join("?" for _ in entity_kinds)
            + ") GROUP BY kind,status",
            entity_kinds,
        ):
            entities[row["kind"]][row["status"]] = row["count"]

        groups = []
        for group, label in AGES.items():
            funnel = published(g.db, group)
            product = None
            material = None
            if funnel and funnel["data"].get("guide_product_id"):
                row = g.db.execute(
                    "SELECT * FROM objects WHERE id=? AND kind='product'",
                    (funnel["data"]["guide_product_id"],),
                ).fetchone()
                product = unpack(row)
            if product and product["data"].get("material_id"):
                row = g.db.execute(
                    "SELECT * FROM objects WHERE id=? AND kind='material'",
                    (product["data"]["material_id"],),
                ).fetchone()
                material = unpack(row)
            groups.append(
                {
                    "age_group": group,
                    "label": label,
                    "funnel": (
                        {
                            "id": funnel["id"],
                            "status": funnel["status"],
                            "revision": funnel["revision"],
                            "contact_configured": bool(funnel["data"].get("contact_url")),
                        }
                        if funnel
                        else None
                    ),
                    "product": (
                        {
                            "id": product["id"],
                            "status": product["status"],
                            "revision": product["revision"],
                            "amount": product["data"].get("amount"),
                            "currency": product["data"].get("currency"),
                            "lava_offer_configured": bool(product["data"].get("lava_offer_id")),
                            "web_sales_approved": product["data"].get("web_sales_approved") is True,
                        }
                        if product
                        else None
                    ),
                    "material": (
                        {
                            "id": material["id"],
                            "status": material["status"],
                            "revision": material["revision"],
                            "asset_configured": bool(material["data"].get("asset_id")),
                        }
                        if material
                        else None
                    ),
                }
            )
        offer_row = g.db.execute(
            "SELECT * FROM objects WHERE kind='template' AND status='published' "
            "AND json_extract(data,'$.type')='offer' ORDER BY updated DESC,rowid DESC LIMIT 1"
        ).fetchone()
        offer = unpack(offer_row) if offer_row else None
        guide_policy = policies(g.db, "commerce", "guide")
        return {
            "integrations": {
                "lava_api": bool(app.config.get("LAVA_API_KEY")),
                "lava_webhook": bool(app.config.get("LAVA_WEBHOOK_SECRET")),
                "public_https": str(app.config.get("PUBLIC_URL", "")).startswith("https://"),
                "external_lava_test_mode": bool(
                    app.config.get("DEV_EXTERNAL_LAVA_CHECKOUT", False)
                ),
                "tripwire_payment_bypass": bool(
                    app.config.get("DEV_TRIPWIRE_BYPASS_PAYMENT", False)
                ),
            },
            "offer": (
                {
                    "id": offer["id"],
                    "status": offer["status"],
                    "revision": offer["revision"],
                    "title": offer["data"].get("title"),
                }
                if offer
                else None
            ),
            "policy": (
                {
                    "id": guide_policy["id"],
                    "status": guide_policy["status"],
                    "revision": guide_policy["revision"],
                    "title": guide_policy["data"].get("title"),
                    "offer_template_id": guide_policy["data"].get("offer_template_id"),
                    "quote_ttl_seconds": guide_policy["data"].get("quote_ttl_seconds"),
                }
                if guide_policy
                else None
            ),
            "groups": groups,
            "entities": entities,
        }

    @app.get("/api/admin/funnel-profile")
    @protected(admin=True)
    def profile():
        row = g.db.execute(
            "SELECT * FROM objects WHERE kind='bot_profile' ORDER BY created DESC,rowid DESC LIMIT 1"
        ).fetchone()
        if row:
            data = unpack(row)["data"]
            return {"name": data["name"], "description": data["description"]}
        return PROFILE

    @app.post("/api/admin/funnel-profile")
    @protected(admin=True)
    def apply_profile():
        d = body()
        why = reason(d)
        require(
            isinstance(d.get("name"), str)
            and 1 <= len(d["name"]) <= 64
            and isinstance(d.get("description"), str)
            and 1 <= len(d["description"]) <= 512,
            "Имя до 64 символов, описание до 512",
        )
        obj = insert(
            g.db,
            "bot_profile",
            {"name": d["name"], "description": d["description"]},
            g.now,
            owner=g.actor["id"],
            status="queued",
        )
        audit(g.db, g.actor["id"], "funnel.profile", obj, g.now, why)
        emit(g.db, g.actor["id"], obj["id"], "funnel.profile", g.now)
        return obj, 202

    @app.get("/api/admin/funnel-runs")
    @protected(admin=True)
    def runs():
        return {
            "items": [
                public_run(unpack(r))
                for r in g.db.execute(
                    "SELECT * FROM objects WHERE kind='funnel_run' ORDER BY created DESC,rowid DESC LIMIT 200"
                )
            ]
        }

    @app.post("/api/admin/funnel-runs/<id>/stop")
    @app.post("/api/admin/funnel-entries/<id>/stop")
    @protected(admin=True)
    def admin_stop(id):
        obj = object_get(g.db, id)
        require(obj["kind"] in ("funnel_run", "funnel_entry"), "Скрининг не найден", 404)
        d = body()
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Скрининг изменён", 409)
        cancel(
            g.db,
            obj,
            ("funnel.age", "funnel.offer", "funnel.cart", "funnel.followup", "funnel.refusal"),
        )
        result = update(g.db, obj, g.now, {**obj["data"], "notifications_stopped": True})
        audit(g.db, g.actor["id"], "funnel.stop", result, g.now, why, obj["revision"])
        return public_run(result)


def apply_profile_job(app, owner, id):
    from .providers import telegram

    now = int(app.config["CLOCK"]())
    with transaction(app.config["DATABASE"]) as db:
        obj = object_get(db, id, "bot_profile", owner)
        newest = db.execute(
            "SELECT id FROM objects WHERE kind='bot_profile' ORDER BY created DESC,rowid DESC LIMIT 1"
        ).fetchone()
        if newest["id"] != id:
            result = update(db, obj, now, status="superseded")
            audit(
                db,
                owner,
                "funnel.profile.superseded",
                result,
                now,
                "Настройка заменена более новой",
                obj["revision"],
            )
            return
    for key, method in (("name", "setMyName"), ("description", "setMyDescription")):
        with connect(app.config["DATABASE"]) as db:
            obj = object_get(db, id, "bot_profile", owner)
            account = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
            require(
                account and account["role"] == "admin" and not account["blocked"],
                "Нужны права специалиста",
            )
            if key in obj["data"].get("applied_fields", []):
                continue
        telegram(app, method, {key: obj["data"][key]})
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, id, "bot_profile", owner)
            fields = sorted(set(current["data"].get("applied_fields", []) + [key]))
            result = update(
                db,
                current,
                now,
                {**current["data"], "applied_fields": fields},
                "applied" if len(fields) == 2 else "partial",
            )
            audit(
                db,
                owner,
                "funnel.profile." + key,
                result,
                now,
                "Применение сохранённой настройки Telegram",
                current["revision"],
            )


def notify_funnel(app, owner, id, event):
    """Recheck state at delivery, so cancellation also wins over queued retries."""
    from .bot import Bot, send_file

    if event == "funnel.profile":
        return apply_profile_job(app, owner, id)
    now = int(app.config["CLOCK"]())
    dev_offer = False
    dev_delivery = False
    with connect(app.config["DATABASE"]) as db:
        obj = object_get(db, id, owner=owner)
        account = db.execute("SELECT * FROM accounts WHERE id=?", (owner,)).fetchone()
        require(account, "Получатель не найден")
        if account["blocked"]:
            return
        d = obj["data"]
        snapshot = d.get("snapshot", {})
        if event == "funnel.age":
            if obj["status"] != "waiting_age" or d.get("notifications_stopped"):
                return
            content = published(db)
            text = content["data"]["age_reminder_text"] if content else AGE_REMINDER
        elif event != "funnel.guide" and (
            obj["status"] != "completed" or d.get("notifications_stopped")
        ):
            return
        if event.startswith(("funnel.cart", "funnel.offer")) and (
            d.get("refused_at")
            or d.get("order_status") == "paid"
            or d.get("dev_tripwire_accepted_at")
            or d.get("booking_id")
        ):
            return
        if event.startswith("funnel.followup") and (
            not d.get("guide_delivered_at") or d.get("booking_id")
        ):
            return
        if event.startswith("funnel.followup"):
            if not d.get("dev_tripwire_accepted_at"):
                right = object_get(db, d.get("guide_right_id"), "right", owner)
                if right["status"] != "active":
                    return
        if event in ("funnel.offer", "funnel.cart.first", "funnel.cart.last"):
            dev_offer = bool(
                event == "funnel.offer"
                and app.config.get("DEV_TRIPWIRE_BYPASS_PAYMENT", False)
                and not snapshot.get("guide_product_id")
            )
            if dev_offer:
                price = "будет указана позже"
            else:
                if not snapshot.get("guide_product_id"):
                    return
                product = object_get(db, snapshot["guide_product_id"], "product")
                external_lava = lava_guide_ready(app, product)
                if product["status"] != "published" or not (
                    product["data"].get("currency") == "XTR" or external_lava
                ):
                    return
                price = price_text(product)
            if event != "funnel.offer" and now >= d.get("offer_expires", 0):
                return
            if event.startswith("funnel.cart"):
                order = object_get(db, d.get("order_id"), "order", owner)
                if order["status"] != "pending":
                    return
        if event == "funnel.guide":
            dev_delivery = bool(d.get("dev_tripwire_accepted_at"))
            if d.get("guide_delivered_at"):
                return
            if dev_delivery:
                blob = {"body": render_guide(snapshot)}
            else:
                right = object_get(db, d.get("guide_right_id"), "right", owner)
                if right["status"] != "active":
                    return
                blob = db.execute(
                    "SELECT body FROM blobs WHERE id=?", (right["data"]["asset_id"],)
                ).fetchone()
                require(blob, "PDF не найден")
        if event == "funnel.refusal":
            if not d.get("refused_at") or d.get("order_status") == "paid" or d.get("booking_id"):
                return
            _, blob = material_asset(
                db, snapshot["refusal_material_id"], ("application/pdf", "video/mp4")
            )
        if event.startswith("funnel.invoice."):
            attempt = object_get(db, event.rsplit(".", 1)[-1], "payment_attempt", owner)
            order = object_get(db, attempt["data"]["order_id"], "order", owner)
            if (
                attempt["status"] != "ready"
                or order["status"] != "pending"
                or order["data"]["expires"] <= now
                or d.get("refused_at")
            ):
                return
    bot = Bot(
        app,
        {"id": account["telegram_id"], "first_name": account["name"]},
        account["telegram_id"],
        "notify:" + id,
    )
    values = {"id": id}
    consult = [("📋 О диагностике", "funnel_consultation", values)]
    if snapshot.get("contact_url"):
        consult.append(("💬 Написать Марине", "url", {"url": snapshot["contact_url"]}))
    if event == "funnel.age":
        bot.send(render_copy(text, {}, account["name"]), bot.funnel_age_buttons(id))
    elif event.startswith("funnel.progress."):
        if d.get("result_sent_at"):
            return
        step = int(event.rsplit(".", 1)[-1])
        require(0 <= step < len(PROGRESS_STEPS), "Некорректный шаг обработки")
        message_id = d.get("progress_message_id")
        if message_id:
            bot.edit(message_id, progress_text(PROGRESS_STEPS[step]))
        else:
            message = bot.send(progress_text(PROGRESS_STEPS[step]))
            message_id = message.get("message_id") if isinstance(message, dict) else None
            require(isinstance(message_id, int), "Telegram не вернул идентификатор сообщения")
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, id)
            update(
                db,
                current,
                now,
                {
                    **current["data"],
                    "progress_message_id": message_id,
                    "progress_percent": PROGRESS_STEPS[step],
                },
            )
    elif event == "funnel.result":
        text = d["result"]["text"] + "\n\n" + d["result"]["limitations"]
        buttons = list(consult)
        if d["result"]["zone"] == "green" and snapshot.get("free_material_id"):
            buttons.insert(0, ("🎁 Игры по возрасту — PDF", "funnel_free", values))
        if d.get("progress_message_id"):
            bot.edit(d["progress_message_id"], text, buttons)
        else:
            bot.send(text, buttons)
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, id)
            update(db, current, now, {**current["data"], "result_sent_at": now})
            if d["result"]["zone"] == "yellow" and (
                snapshot.get("guide_product_id")
                or app.config.get("DEV_TRIPWIRE_BYPASS_PAYMENT", False)
            ):
                schedule(db, current, "funnel.offer", now + 2)
    elif event == "funnel.offer":
        expires = d.get("offer_expires", now + 7200)
        if expires <= now:
            return
        buttons = (
            [
                ("✅ Принять", "funnel_accept_dev", values),
                ("❌ Отменить", "funnel_refuse", values),
            ]
            if dev_offer
            else [
                ("💳 Забрать руководство за " + price, "funnel_buy", values),
                ("❌ Отказаться и закрыть доступ", "funnel_refuse", values),
            ]
        )
        bot.send(
            render_copy(snapshot["offer_text"], snapshot, account["name"], price=price),
            buttons,
        )
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, id)
            update(db, current, now, {**current["data"], "offer_expires": expires})
    elif event.startswith("funnel.cart"):
        field = "cart_first_text" if event.endswith("first") else "cart_last_text"
        text = snapshot[field]
        if event.endswith("last") and now > d["offer_expires"] - 840:
            text = re.sub("через 15 минут", "скоро", text, flags=re.I)
        bot.send(
            render_copy(text, snapshot, account["name"], price=price),
            [
                ("💳 Завершить оплату", "funnel_buy", values),
                ("🆘 Помощь с оплатой", "funnel_consultation", values),
            ],
        )
    elif event == "funnel.guide":
        if not d.get("guide_file_sent_at"):
            send_file(
                app,
                account["telegram_id"],
                snapshot["guide_filename"],
                blob["body"],
                "application/pdf",
            )
            with transaction(app.config["DATABASE"]) as db:
                current = object_get(db, id)
                update(db, current, now, {**current["data"], "guide_file_sent_at": now})
        bot.send(
            (
                "Тестовый PDF готов. Оплата не выполнялась."
                if dev_delivery
                else render_copy(snapshot["success_text"], snapshot, account["name"])
            ),
            consult,
        )
        with transaction(app.config["DATABASE"]) as db:
            current = object_get(db, id)
            current = update(
                db,
                current,
                now,
                {
                    **current["data"],
                    "guide_delivered_at": now,
                    "consultation_expires": now + snapshot["consultation_valid_days"] * 86400,
                },
            )
            if not current["data"].get("notifications_stopped") and not current["data"].get(
                "booking_id"
            ):
                for f in snapshot["followups"]:
                    schedule(
                        db, current, "funnel.followup." + str(f["hours"]), now + f["hours"] * 3600
                    )
    elif event.startswith("funnel.followup"):
        hours = int(event.rsplit(".", 1)[-1])
        # Do not send obsolete countdowns after downtime.
        if now > d["consultation_expires"] + 300 or (
            hours < 72 and now >= d["guide_delivered_at"] + (hours + 24) * 3600
        ):
            return
        deadline = datetime.fromtimestamp(
            d["consultation_expires"], ZoneInfo(snapshot["timezone"])
        ).strftime("%d.%m.%Y в %H:%M")
        text = next(f["text"] for f in snapshot["followups"] if f["hours"] == hours)
        bot.send(render_copy(text, snapshot, account["name"], deadline=deadline), consult)
    elif event == "funnel.refusal":
        video = blob["mime"] == "video/mp4"
        send_file(
            app,
            account["telegram_id"],
            "activity.mp4" if video else "checklist.pdf",
            blob["body"],
            blob["mime"],
            "sendVideo" if video else "sendDocument",
        )
    elif event.startswith("funnel.invoice."):
        lava_checkout = attempt["data"]["provider"] == "lava"
        bot.send(
            (
                "Счёт готов. Оплатите на защищённой странице Lava.top:"
                if lava_checkout
                else "Счёт готов. Оплатите в Telegram Stars:"
            ),
            [("💳 Оплатить", "url", {"url": attempt["data"]["url"]})],
        )
