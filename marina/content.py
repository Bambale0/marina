from flask import g, request
from . import body, object_get, offset, protected, reason, require
from .db import audit, insert, unpack, update

KINDS = {
    "onboarding",
    "decision",
    "material",
    "method",
    "product",
    "policy",
    "exercise",
    "selection",
    "course",
    "template",
}


def register_content(app):
    @app.get("/api/admin/onboarding-defaults/<age_group>")
    @protected(admin=True)
    def onboarding_defaults(age_group):
        from .onboarding import AGE_GROUPS, QUESTIONS

        require(age_group in AGE_GROUPS, "Выберите возрастную группу")
        return {
            "title": "Знакомство: " + AGE_GROUPS[age_group][0],
            "age_group": age_group,
            "concerns": [
                {"id": str(i), "text": text} for i, text in enumerate(QUESTIONS[age_group], 1)
            ],
        }

    @app.get("/api/admin/content")
    @protected(admin=True)
    def list_content():
        kind = request.args.get("kind")
        term = "%" + request.args.get("q", "")[:100] + "%"
        rows = g.db.execute(
            "SELECT * FROM objects WHERE kind IN ("
            + ",".join("?" for _ in KINDS)
            + ") AND status!='deleted' AND (? IS NULL OR kind=?) AND data LIKE ? ORDER BY created,id LIMIT 100 OFFSET ?",
            (*sorted(KINDS), kind, kind, term, offset()),
        )
        return {"items": [unpack(r) for r in rows]}

    @app.post("/api/admin/content")
    @protected(admin=True)
    def create_content():
        data = body()
        why = reason(data)
        require(data.get("kind") in KINDS, "Неизвестный тип материала")
        require(
            isinstance(data.get("title"), str) and 1 <= len(data["title"].strip()) <= 200,
            "Укажите название",
        )
        require(isinstance(data.get("data", {}), dict), "Неверные поля материала")
        obj = insert(
            g.db,
            data["kind"],
            {**data.get("data", {}), "title": data["title"].strip()},
            g.now,
        )
        audit(g.db, g.actor["id"], "content.create", obj, g.now, why)
        return obj, 201

    @app.get("/api/admin/content/<id>")
    @protected(admin=True)
    def read_content(id):
        obj = object_get(g.db, id)
        require(obj["kind"] in KINDS, "Материал не найден", 404)
        return obj

    @app.patch("/api/admin/content/<id>")
    @protected(admin=True)
    def edit_content(id):
        obj = object_get(g.db, id)
        data = body()
        why = reason(data)
        require(
            obj["kind"] in KINDS and obj["status"] == "draft",
            "Измените новую черновую версию",
            409,
        )
        require(
            data.get("revision") == obj["revision"],
            "Версия изменена, обновите страницу",
            409,
        )
        payload = data.get("data", obj["data"])
        require(isinstance(payload, dict), "Неверные поля")
        payload = {**payload, "title": data.get("title", obj["data"]["title"])}
        require(
            isinstance(payload["title"], str) and 1 <= len(payload["title"].strip()) <= 200,
            "Укажите название",
        )
        payload.pop("approval", None)
        updated = update(g.db, obj, g.now, payload)
        audit(g.db, g.actor["id"], "content.edit", updated, g.now, why, obj["revision"])
        return updated

    @app.get("/api/admin/content/<id>/preview")
    @protected(admin=True)
    def preview_content(id):
        obj = object_get(g.db, id)
        require(obj["kind"] in KINDS, "Материал не найден", 404)
        validate(obj)
        examples = []
        if obj["kind"] == "method":
            from .screening import score

            examples = [
                {
                    "answers": ex["answers"],
                    "expected": ex["score"],
                    "actual": score(obj["data"], ex["answers"]),
                }
                for ex in obj["data"]["examples"]
            ]
        return {
            "valid": True,
            "version": obj["revision"],
            "examples": examples,
            "content": obj["data"],
        }

    @app.post("/api/admin/content/<id>/<action>")
    @protected(admin=True)
    def content_action(id, action):
        obj = object_get(g.db, id)
        require(obj["kind"] in KINDS, "Материал не найден", 404)
        data = body()
        why = reason(data)
        require(
            data.get("revision") == obj["revision"],
            "Версия изменена, обновите страницу",
            409,
        )
        payload = {**obj["data"]}
        if action == "copy":
            payload.pop("approval", None)
            payload["previous_version"] = obj["id"]
            result = insert(g.db, obj["kind"], payload, g.now)
        elif action == "approve":
            require(obj["status"] == "draft", "Утверждать можно только черновик", 409)
            validate(obj)
            payload["approval"] = {
                "actor": g.actor["id"],
                "at": g.now,
                "revision": obj["revision"],
            }
            result = update(g.db, obj, g.now, payload, "approved")
        elif action == "publish":
            require(
                obj["status"] == "approved",
                "Сначала утвердите конкретную редакцию",
                409,
            )
            validate(obj)
            previous_id = payload.get("previous_version")
            visited = set()
            while previous_id and previous_id not in visited and len(visited) < 100:
                visited.add(previous_id)
                previous = object_get(g.db, previous_id, obj["kind"])
                if previous["status"] == "published":
                    archived = update(g.db, previous, g.now, status="archived")
                    audit(
                        g.db,
                        g.actor["id"],
                        "content.superseded",
                        archived,
                        g.now,
                        why,
                        previous["revision"],
                    )
                previous_id = previous["data"].get("previous_version")
            result = update(g.db, obj, g.now, status="published")
        elif action == "delete":
            require(
                obj["status"] == "draft",
                "Удалить можно только неиспользованный черновик",
                409,
            )
            result = update(g.db, obj, g.now, status="deleted")
        elif action == "archive":
            require(obj["status"] != "archived", "Уже архивировано", 409)
            result = update(g.db, obj, g.now, status="archived")
        else:
            require(False, "Неизвестное действие", 404)
        audit(
            g.db,
            g.actor["id"],
            "content." + action,
            result,
            g.now,
            why,
            obj["revision"],
        )
        return result

    @app.post("/api/admin/content/<id>/asset")
    @protected(admin=True)
    def upload_asset(id):
        import hashlib
        from .db import uid

        obj = object_get(g.db, id)
        require(
            obj["kind"] in ("exercise", "material") and obj["status"] == "draft",
            "Файлы добавляются к черновику",
        )
        why = reason({"reason": request.form.get("reason", "")})
        file = request.files.get("file")
        require(file, "Выберите файл")
        data = file.read(20 * 1024 * 1024 + 1)
        require(0 < len(data) <= 20 * 1024 * 1024, "Лимит файла 20 МБ")
        if obj["kind"] == "exercise":
            require(file.mimetype == "video/mp4" and data[4:8] == b"ftyp", "Нужен MP4-файл")
            from .media import verify_video

            verify_video(data)
        else:
            require(
                file.mimetype in ("application/pdf", "text/plain", "video/mp4"),
                "Разрешены PDF, текст и MP4",
            )
        id = uid()
        g.db.execute(
            "INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?)",
            (
                id,
                g.actor["id"],
                "",
                obj["id"],
                file.mimetype,
                hashlib.sha256(data).hexdigest(),
                data,
                g.now,
            ),
        )
        audit(g.db, g.actor["id"], "content.asset", {"id": id}, g.now, why)
        return {"id": id, "mime": file.mimetype}, 201

    @app.get("/api/catalog/<kind>")
    @protected()
    def catalog(kind):
        require(
            kind in ("method", "product", "exercise", "course"),
            "Каталог не найден",
            404,
        )
        rows = [
            unpack(r)
            for r in g.db.execute(
                "SELECT * FROM objects WHERE kind=? AND status='published' ORDER BY created,id",
                (kind,),
            )
        ]
        subject_id = request.args.get("subject_id")
        if subject_id and kind in ("method", "course"):
            from . import age_months

            subject = object_get(g.db, subject_id, "subject", g.actor["id"])
            months = age_months(subject["data"]["birth_date"], g.now)
            rows = [
                o
                for o in rows
                if o["data"].get("audience") == subject["data"]["kind"]
                and o["data"].get("min_months", 0) <= months <= o["data"].get("max_months", 1440)
            ]
        from .views import catalog_view

        return {"items": [catalog_view(o) for o in rows]}


def validate(obj):
    d = obj["data"]
    if obj["kind"] == "onboarding":
        from .onboarding import validate_screen

        validate_screen(d)
    if obj["kind"] in ("decision", "material"):
        require(
            d.get("owner") and d.get("status"),
            "Укажите владельца и состояние готовности",
        )
    if obj["kind"] == "method":
        from .screening import validate_method

        validate_method(d)
    if obj["kind"] == "product":
        require(
            d.get("service") in ("pdf", "case", "subscription", "course", "booking"),
            "Выберите услугу",
        )
        require(
            type(d.get("amount")) is int
            and d["amount"] > 0
            and d.get("currency") in ("RUB", "XTR"),
            "Укажите цену в копейках RUB или целых XTR",
        )
        require(d.get("terms"), "Укажите условия покупки")
        if d["service"] in ("subscription", "course"):
            require(
                type(d.get("period_hours")) is int and d["period_hours"] > 0,
                "Укажите срок доступа",
            )
        require(d.get("launch_approved") is True, "Подтвердите выполнение условий запуска")
    if obj["kind"] == "product" and d.get("service") == "case":
        require(
            isinstance(d.get("case_questions"), list) and d["case_questions"],
            "Добавьте вопросы анкеты разбора",
        )
        seen = set()
        for q in d["case_questions"]:
            require(
                isinstance(q, dict)
                and isinstance(q.get("id"), str)
                and q["id"] not in seen
                and q.get("text"),
                "Нужны уникальные ID и тексты вопросов",
            )
            seen.add(q["id"])
    if obj["kind"] in ("exercise", "course", "selection"):
        require(d.get("source") and d.get("instructions"), "Нужны источник и инструкция")

    if obj["kind"] == "policy":
        require(
            d.get("type") in ("commerce", "operations", "privacy", "training", "ai"),
            "Укажите тип правил",
        )
        require(d.get("terms"), "Укажите условия и основание решения")
        if d["type"] == "commerce":
            require(
                d.get("family_at") == "quote"
                and type(d.get("include_future")) is bool
                and d.get("rank") == "first_paid_then_created"
                and d.get("rounding") == "half_up",
                "Выберите поддерживаемые правила семьи и округления",
            )
            require(
                d.get("trial_purchase") in ("immediately", "after_trial")
                and d.get("late_payment") == "review",
                "Укажите правило trial и поздней оплаты",
            )
            require(
                all(
                    type(d.get(k)) is int and d[k] > 0
                    for k in (
                        "quote_ttl_seconds",
                        "renewal_window_hours",
                        "max_future_periods",
                    )
                ),
                "Укажите срок расчёта, окно продления и лимит будущих периодов",
            )

    if obj["kind"] == "exercise":
        require(
            all(
                type(d.get(k)) is int
                for k in ("duration_seconds", "min_months", "max_months", "difficulty")
            )
            and 1 <= d["duration_seconds"] <= 900
            and 36 <= d["min_months"] <= d["max_months"] <= 132,
            "Укажите длительность, сложность и возраст 3–10 лет в месяцах",
        )
        require(
            d.get("rights") and d.get("video_id") and d.get("video_verified") is True,
            "Нужны права, сохранённое видео и подтверждение просмотра",
        )
        require(
            g.db.execute("SELECT 1 FROM blobs WHERE id=?", (d["video_id"],)).fetchone(),
            "Видео не сохранено",
        )
        require(
            all(
                isinstance(d.get(k, []), list) and all(isinstance(v, str) for v in d.get(k, []))
                for k in ("equipment", "exclusions")
            ),
            "Оборудование и ограничения должны быть списками",
        )
    if obj["kind"] == "selection":
        require(
            type(d.get("min_seconds")) is int
            and type(d.get("max_seconds")) is int
            and 600 <= d["min_seconds"] <= d["max_seconds"] <= 900
            and type(d.get("difficulty")) is int
            and d.get("examples"),
            "Нужны длительность 10–15 минут, сложность и контрольные примеры",
        )
    if obj["kind"] == "selection":
        allowed = d.get("allowed_difficulties") or [d["difficulty"]]
        require(
            isinstance(allowed, list)
            and all(type(v) is int for v in allowed)
            and d["difficulty"] in allowed,
            "Задайте допустимые уровни сложности",
        )
        mapping = d.get("adaptation", {})
        require(
            isinstance(mapping, dict)
            and set(mapping) <= {"easy", "hard", "suitable", "stop"}
            and all(type(v) is int and v in allowed for v in mapping.values()),
            "Адаптация должна оставаться в утверждённых уровнях",
        )
    if obj["kind"] == "course":
        require(
            d.get("rights")
            and d.get("audience") in ("adult", "child")
            and type(d.get("min_months")) is int
            and type(d.get("max_months")) is int
            and d["min_months"] <= d["max_months"],
            "Укажите права и аудиторию курса",
        )
        require(isinstance(d.get("lessons"), list) and d["lessons"], "Добавьте уроки")
        seen = set()
        for lesson in d["lessons"]:
            require(
                isinstance(lesson, dict)
                and isinstance(lesson.get("id"), str)
                and lesson["id"] not in seen
                and lesson.get("title")
                and lesson.get("text"),
                "Каждый урок должен иметь уникальный ID, название и материал",
            )
            require(
                set(lesson.get("requires", [])) <= seen,
                "Предпосылки урока должны ссылаться на предыдущие уроки",
            )
            for id in lesson.get("exercise_ids", []):
                exercise = object_get(g.db, id, "exercise")
                require(
                    exercise["status"] == "published",
                    "Урок ссылается на неопубликованное упражнение",
                )
            seen.add(lesson["id"])
        modules = d.get("modules", [])
        require(isinstance(modules, list), "Неверные модули")
        module_ids = set()
        assigned = []
        for module in modules:
            require(
                isinstance(module, dict)
                and isinstance(module.get("id"), str)
                and module["id"] not in module_ids
                and module.get("title"),
                "Нужны уникальные ID и названия модулей",
            )
            require(
                isinstance(module.get("lesson_ids"), list)
                and module["lesson_ids"]
                and set(module["lesson_ids"]) <= seen,
                "Модуль должен содержать существующие уроки",
            )
            require(
                set(module.get("requires", [])) <= module_ids,
                "Предпосылки должны ссылаться на предыдущие модули",
            )
            module_ids.add(module["id"])
            assigned.extend(module["lesson_ids"])
        if modules:
            require(
                len(assigned) == len(set(assigned)) and set(assigned) == seen,
                "Каждый урок должен входить ровно в один модуль",
            )

    if obj["kind"] == "policy" and d["type"] == "training":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(d.get("timezone", ""))
        except (ValueError, ZoneInfoNotFoundError):
            require(False, "Нужен часовой пояс IANA")
        require(
            d.get("trial_hours") == 72 and d.get("expiry") == "block",
            "Подтвердите 72 часа trial и остановку доступа при истечении",
        )

    if obj["kind"] == "policy" and d["type"] == "ai":
        require(
            isinstance(d.get("model"), str)
            and d["model"].strip()
            and type(d.get("max_output_tokens")) is int
            and 1 <= d["max_output_tokens"] <= 10000
            and type(d.get("daily_jobs")) is int
            and 1 <= d["daily_jobs"] <= 1000,
            "Задайте модель и лимиты ИИ",
        )
    if obj["kind"] == "policy" and d["type"] == "privacy":
        require(
            type(d.get("media_max_bytes")) is int
            and 1 <= d["media_max_bytes"] <= 20 * 1024 * 1024
            and type(d.get("retention_days")) is int
            and d["retention_days"] > 0,
            "Укажите лимит файлов до 20 МБ и срок хранения",
        )
    if obj["kind"] == "template":
        require(
            d.get("type") and isinstance(d.get("text"), str) and d["text"].strip(),
            "Укажите тип и текст шаблона",
        )

    if obj["kind"] == "policy" and d["type"] == "operations":
        require(
            type(d.get("pause_sales", True)) is bool
            and type(d.get("max_open_orders", 0)) is int
            and d.get("max_open_orders", 0) >= 0,
            "Неверные лимиты приёма",
        )
        for k in ("sla_reminder_hours", "renewal_reminder_hours"):
            require(
                isinstance(d.get(k, []), list)
                and all(type(v) is int and 0 <= v <= 8760 for v in d.get(k, [])),
                "Напоминания задаются целыми часами до срока",
            )
