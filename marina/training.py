"""Published-content selection, family trial and course progress."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import g, request, Response
from . import age_months, body, object_get, protected, reason, require
from .commerce import policies, rights
from .db import audit, emit, insert, unpack, update
from .screening import idempotent


def active_access(db, owner, subject, now):
    return any(
        r["data"].get("service") == "subscription"
        and r["data"]["starts"] <= now < r["data"]["ends"]
        for r in rights(db, owner, subject)
    )


def select(db, subject, equipment, constraints, now):
    months = age_months(subject["data"]["birth_date"], now)
    rules = [
        unpack(r)
        for r in db.execute(
            "SELECT * FROM objects WHERE kind='selection' AND status='published' ORDER BY created DESC,rowid DESC"
        )
    ]
    require(rules, "Правила подбора ещё не опубликованы")
    rule = rules[0]
    difficulty = rule["data"]["difficulty"]
    last = db.execute(
        "SELECT * FROM objects WHERE kind='lesson' AND subject=? AND status='completed' ORDER BY updated DESC,rowid DESC LIMIT 1",
        (subject["id"],),
    ).fetchone()
    adaptation = None
    if last:
        previous = unpack(last)
        feedback = previous["data"].get("feedback")
        mapping = rule["data"].get("adaptation", {})
        if feedback in mapping:
            difficulty = mapping[feedback]
            adaptation = {
                "previous_lesson": previous["id"],
                "feedback": feedback,
                "difficulty": difficulty,
            }
    allowed, excluded = [], []
    for row in db.execute(
        "SELECT * FROM objects WHERE kind='exercise' AND status='published' ORDER BY created,id"
    ):
        ex = unpack(row)
        d = ex["data"]
        reasons = []
        if not d["min_months"] <= months <= d["max_months"]:
            reasons.append("age")
        if not set(d.get("equipment", [])) <= set(equipment):
            reasons.append("equipment")
        if set(d.get("exclusions", [])) & set(constraints):
            reasons.append("contraindication")
        if d["difficulty"] != difficulty:
            reasons.append("difficulty")
        if reasons:
            excluded.append({"id": ex["id"], "reasons": reasons})
        else:
            allowed.append(ex)
    # Bounded subset-sum: deterministic sequence within the expert-approved duration.
    plans = {0: []}
    for ex in allowed[:200]:
        for length, plan in list(plans.items()):
            new = length + ex["data"]["duration_seconds"]
            if new <= rule["data"]["max_seconds"] and new not in plans:
                plans[new] = [*plan, ex]
    valid = [
        duration
        for duration in plans
        if rule["data"]["min_seconds"] <= duration <= rule["data"]["max_seconds"]
    ]
    require(valid, "Недостаточно утверждённых материалов для подходящего комплекса")
    duration = min(valid)
    return {
        "difficulty": difficulty,
        "adaptation": adaptation,
        "rule_id": rule["id"],
        "rule_snapshot": rule["data"],
        "exercises": [{"id": ex["id"], **ex["data"]} for ex in plans[duration]],
        "duration_seconds": duration,
        "exclusions": excluded,
        "equipment": equipment,
        "constraints": constraints,
    }


def validate_plan(db, plan, subject, equipment, constraints, now):
    months = age_months(subject["data"]["birth_date"], now)
    for snapshot in plan["exercises"]:
        ex = object_get(db, snapshot["id"], "exercise")
        require(
            ex["status"] == "published",
            "В плане есть снятый материал. Нужна проверка специалиста",
        )
        require(
            snapshot["min_months"] <= months <= snapshot["max_months"]
            and set(snapshot.get("equipment", [])) <= set(equipment)
            and not set(snapshot.get("exclusions", [])) & set(constraints),
            "План не соответствует текущему возрасту, предметам или ограничениям",
        )
    require(
        600 <= sum(ex["duration_seconds"] for ex in plan["exercises"]) <= 900,
        "Комплекс должен длиться 10–15 минут",
    )


def register_training(app):
    def prepare_plan(owner_check=True):
        d = body()
        subject = object_get(
            g.db, d.get("subject_id"), "subject", g.actor["id"] if owner_check else None
        )
        require(
            subject["status"] == "active" and subject["data"]["kind"] == "child",
            "Выберите активный детский профиль",
        )
        equipment, constraints = d.get("equipment", []), d.get("constraints", [])
        require(
            isinstance(equipment, list)
            and isinstance(constraints, list)
            and all(isinstance(v, str) for v in equipment + constraints),
            "Неверные условия подбора",
        )

        def create():
            plan = select(g.db, subject, equipment, constraints, g.now)
            obj = insert(g.db, "plan", plan, g.now, subject["owner"], subject["id"], "prepared")
            audit(
                g.db,
                g.actor["id"],
                "plan.prepare",
                obj,
                g.now,
                "Подготовлен будущий комплекс",
            )
            return obj

        return idempotent(
            g.db,
            "plan:" + g.actor["id"],
            d.get("key"),
            {
                "subject_id": subject["id"],
                "equipment": equipment,
                "constraints": constraints,
            },
            create,
        )

    @app.post("/api/plans")
    @protected()
    def own_plan():
        return prepare_plan()

    @app.post("/api/admin/plans")
    @protected(admin=True)
    def admin_plan():
        return prepare_plan(False)

    @app.post("/api/admin/plans/<id>/replace")
    @protected(admin=True)
    def replace_exercise(id):
        obj = object_get(g.db, id, "plan")
        d = body()
        why = reason(d)
        require(
            obj["status"] == "prepared" and d.get("revision") == obj["revision"],
            "План уже изменён или начат",
            409,
        )
        original = next(
            (ex for ex in obj["data"]["exercises"] if ex["id"] == d.get("exercise_id")),
            None,
        )
        require(
            original and d.get("replacement_id") in original.get("alternatives", []),
            "Замена не утверждена в исходном упражнении",
        )
        require(
            d["replacement_id"] not in {ex["id"] for ex in obj["data"]["exercises"]},
            "Упражнение уже включено",
        )
        replacement = object_get(g.db, d["replacement_id"], "exercise")
        require(
            replacement["status"] == "published"
            and replacement["data"]["difficulty"]
            == obj["data"].get("difficulty", obj["data"]["rule_snapshot"]["difficulty"]),
            "Замена не соответствует правилам",
        )
        payload = {
            **obj["data"],
            "exercises": [
                {"id": replacement["id"], **replacement["data"]}
                if ex["id"] == original["id"]
                else ex
                for ex in obj["data"]["exercises"]
            ],
        }
        subject = object_get(g.db, obj["subject"], "subject")
        validate_plan(g.db, payload, subject, payload["equipment"], payload["constraints"], g.now)
        payload["duration_seconds"] = sum(ex["duration_seconds"] for ex in payload["exercises"])
        result = update(g.db, obj, g.now, payload)
        audit(g.db, g.actor["id"], "plan.replace", result, g.now, why, obj["revision"])
        return result

    @app.post("/api/lessons/start")
    @protected()
    def start_lesson():
        d = body()
        s = object_get(g.db, d.get("subject_id"), "subject", g.actor["id"])
        require(
            s["status"] == "active" and s["data"]["kind"] == "child",
            "Выберите действующий детский профиль",
        )
        equipment, constraints = d.get("equipment", []), d.get("constraints", [])
        require(
            isinstance(equipment, list)
            and isinstance(constraints, list)
            and all(isinstance(v, str) for v in equipment + constraints),
            "Неверные условия подбора",
        )
        policy = policies(g.db, "training")
        require(policy, "Условия занятий ещё не опубликованы")
        day = datetime.fromtimestamp(g.now, ZoneInfo(policy["data"]["timezone"])).date().isoformat()

        def start():
            old = g.db.execute(
                "SELECT * FROM objects WHERE kind='lesson' AND subject=? AND json_extract(data,'$.day')=?",
                (s["id"], day),
            ).fetchone()
            if old:
                return unpack(old)
            prepared = None
            if d.get("plan_id"):
                prepared = object_get(g.db, d["plan_id"], "plan", g.actor["id"])
                require(
                    prepared["status"] == "prepared" and prepared["subject"] == s["id"],
                    "План уже использован или относится к другому профилю",
                )
            else:
                row = g.db.execute(
                    "SELECT * FROM objects WHERE kind='plan' AND subject=? AND status='prepared' ORDER BY created DESC,rowid DESC LIMIT 1",
                    (s["id"],),
                ).fetchone()
                if row:
                    prepared = unpack(row)
            plan = prepared["data"] if prepared else select(g.db, s, equipment, constraints, g.now)
            if prepared:
                validate_plan(g.db, plan, s, equipment, constraints, g.now)
                plan = {**plan, "plan_id": prepared["id"]}
            if not active_access(g.db, g.actor["id"], s["id"], g.now):
                g.db.execute(
                    "INSERT OR IGNORE INTO trials VALUES(?,?,?)",
                    (g.actor["id"], g.now, g.now + 72 * 3600),
                )
                trial = g.db.execute(
                    "SELECT * FROM trials WHERE account_id=?", (g.actor["id"],)
                ).fetchone()
                require(
                    g.now < trial["ends"],
                    "Пробный период завершён. Выберите подписку для ребёнка",
                    403,
                )
            obj = insert(
                g.db,
                "lesson",
                {
                    **plan,
                    "day": day,
                    "policy_snapshot": policy["data"],
                    "started_at": g.now,
                    "completed_exercises": [],
                },
                g.now,
                g.actor["id"],
                s["id"],
                "started",
            )
            if prepared:
                update(g.db, prepared, g.now, status="started")
            audit(
                g.db,
                g.actor["id"],
                "lesson.start",
                obj,
                g.now,
                "Первый запуск готового комплекса",
            )
            return obj

        return idempotent(
            g.db,
            "lesson:" + g.actor["id"],
            d.get("key"),
            {"subject_id": s["id"], "day": day},
            start,
        )

    def lesson_access(obj):
        if active_access(g.db, g.actor["id"], obj["subject"], g.now):
            return
        trial = g.db.execute("SELECT * FROM trials WHERE account_id=?", (g.actor["id"],)).fetchone()
        require(trial and g.now < trial["ends"], "Доступ к занятиям завершён", 403)

    @app.get("/api/lessons/<id>")
    @protected()
    def read_lesson(id):
        obj = object_get(g.db, id, "lesson", g.actor["id"])
        lesson_access(obj)
        return obj

    @app.patch("/api/lessons/<id>/progress")
    @protected()
    def save_progress(id):
        obj = object_get(g.db, id, "lesson", g.actor["id"])
        lesson_access(obj)
        d = body()
        require(
            obj["status"] == "started" and d.get("revision") == obj["revision"],
            "Прогресс уже изменён",
            409,
        )
        completed = d.get("completed_exercises")
        require(
            isinstance(completed, list)
            and all(isinstance(v, str) for v in completed)
            and set(completed) <= {e["id"] for e in obj["data"]["exercises"]},
            "Неверные отметки упражнений",
        )
        return update(
            g.db,
            obj,
            g.now,
            {**obj["data"], "completed_exercises": list(dict.fromkeys(completed))},
        )

    @app.post("/api/lessons/<id>/complete")
    @protected()
    def complete_lesson(id):
        obj = object_get(g.db, id, "lesson", g.actor["id"])
        if obj["status"] == "completed":
            return obj
        lesson_access(obj)
        d = body()
        completed = d.get("completed_exercises")
        require(
            isinstance(completed, list)
            and set(completed) == {e["id"] for e in obj["data"]["exercises"]},
            "Отметьте выполнение всех упражнений",
        )
        require(
            d.get("feedback") in ("easy", "suitable", "hard", "stop"),
            "Укажите обратную связь",
        )
        result = update(
            g.db,
            obj,
            g.now,
            {
                **obj["data"],
                "completed_exercises": completed,
                "feedback": d["feedback"],
                "completed_at": g.now,
            },
            "completed",
        )
        # No automatic difficulty increase without a separately approved adaptation rule.
        audit(
            g.db,
            g.actor["id"],
            "lesson.complete",
            result,
            g.now,
            "Завершение занятия",
            obj["revision"],
        )
        if d["feedback"] == "stop":
            emit(g.db, obj["owner"], id, "lesson.review_required", g.now)
        return result

    @app.get("/api/lessons/<id>/video/<exercise_id>")
    @protected()
    def lesson_video(id, exercise_id):
        obj = object_get(g.db, id, "lesson", g.actor["id"])
        lesson_access(obj)
        ex = next((e for e in obj["data"]["exercises"] if e["id"] == exercise_id), None)
        require(ex, "Упражнение не найдено", 404)
        blob = g.db.execute("SELECT * FROM blobs WHERE id=?", (ex["video_id"],)).fetchone()
        require(blob, "Видео недоступно", 404)
        return Response(
            blob["body"],
            mimetype="video/mp4",
            headers={"Content-Disposition": 'inline; filename="exercise.mp4"'},
        )

    @app.post("/api/admin/selection/preview")
    @protected(admin=True)
    def preview_selection():
        d = body()
        s = object_get(g.db, d.get("subject_id"), "subject")
        return select(g.db, s, d.get("equipment", []), d.get("constraints", []), g.now)

    @app.post("/api/admin/lessons/<id>/correct")
    @protected(admin=True)
    def correct_lesson(id):
        obj = object_get(g.db, id, "lesson")
        d = body()
        why = reason(d)
        require(d.get("revision") == obj["revision"], "Занятие изменено", 409)
        correction = insert(
            g.db,
            "correction",
            {"lesson_id": id, "note": why, "actor": g.actor["id"]},
            g.now,
            obj["owner"],
            obj["subject"],
            "recorded",
        )
        audit(g.db, g.actor["id"], "lesson.correction", correction, g.now, why)
        return correction

    @app.get("/api/my-courses")
    @protected()
    def my_courses():
        from .views import catalog_view

        items = []
        for right in rights(g.db, g.actor["id"]):
            if right["data"].get("service") != "course":
                continue
            course = object_get(g.db, right["data"]["course_id"], "course")
            if course["status"] in ("published", "archived"):
                items.append(
                    {
                        "course": catalog_view(course),
                        "right": right,
                        "accessible": right["data"]["starts"] <= g.now < right["data"]["ends"],
                    }
                )
        return {"items": items}

    def prerequisites(course, lesson, completed):
        required = set(lesson.get("requires", []))
        modules = {m["id"]: m for m in course["data"].get("modules", [])}
        module = next(
            (m for m in modules.values() if lesson["id"] in m.get("lesson_ids", [])), None
        )
        if module:
            for id in module.get("requires", []):
                required.update(modules[id]["lesson_ids"])
        require(required <= set(completed), "Сначала завершите предыдущие уроки и модули", 403)

    def course_access(course_id, subject_id):
        s = object_get(g.db, subject_id, "subject", g.actor["id"])
        r = next(
            (
                r
                for r in rights(g.db, g.actor["id"], s["id"])
                if r["data"].get("service") == "course"
                and r["data"].get("course_id") == course_id
                and r["data"]["starts"] <= g.now < r["data"]["ends"]
            ),
            None,
        )
        require(r, "Нет оплаченного доступа к этому курсу", 403)
        course = object_get(g.db, course_id, "course")
        require(course["status"] in ("published", "archived"), "Версия курса недоступна", 403)
        return course

    def course_progress(course_id, subject_id):
        row = g.db.execute(
            "SELECT * FROM objects WHERE kind='progress' AND owner=? AND subject=? AND json_extract(data,'$.course_id')=?",
            (g.actor["id"], subject_id, course_id),
        ).fetchone()
        return (
            unpack(row)
            if row
            else insert(
                g.db,
                "progress",
                {"course_id": course_id, "completed": []},
                g.now,
                g.actor["id"],
                subject_id,
                "active",
            )
        )

    @app.get("/api/courses/<course_id>/lessons/<lesson_id>")
    @protected()
    def course_lesson(course_id, lesson_id):
        s = request.args.get("subject_id")
        course = course_access(course_id, s)
        lesson = next(
            (lesson for lesson in course["data"]["lessons"] if lesson["id"] == lesson_id),
            None,
        )
        require(lesson, "Урок не найден", 404)
        progress = course_progress(course_id, s)
        prerequisites(course, lesson, progress["data"]["completed"])
        progress = update(g.db, progress, g.now, {**progress["data"], "current_lesson": lesson_id})
        exercises = []
        for id in lesson.get("exercise_ids", []):
            ex = object_get(g.db, id, "exercise")
            exercises.append(
                {"id": id, "title": ex["data"]["title"], "instructions": ex["data"]["instructions"]}
            )
        return {
            **lesson,
            "exercises": exercises,
            "completed": lesson_id in progress["data"]["completed"],
        }

    @app.get("/api/courses/<course_id>/lessons/<lesson_id>/videos/<exercise_id>")
    @protected()
    def course_video(course_id, lesson_id, exercise_id):
        s = request.args.get("subject_id")
        course = course_access(course_id, s)
        lesson = next((item for item in course["data"]["lessons"] if item["id"] == lesson_id), None)
        require(
            lesson and exercise_id in lesson.get("exercise_ids", []),
            "Видео не включено в урок",
            404,
        )
        progress = course_progress(course_id, s)
        prerequisites(course, lesson, progress["data"]["completed"])
        ex = object_get(g.db, exercise_id, "exercise")
        blob = g.db.execute("SELECT * FROM blobs WHERE id=?", (ex["data"]["video_id"],)).fetchone()
        require(blob, "Видео недоступно", 404)
        return Response(blob["body"], mimetype="video/mp4")

    @app.post("/api/courses/<course_id>/lessons/<lesson_id>/complete")
    @protected()
    def complete_course_lesson(course_id, lesson_id):
        s = body().get("subject_id")
        course = course_access(course_id, s)
        lesson = next(
            (lesson for lesson in course["data"]["lessons"] if lesson["id"] == lesson_id),
            None,
        )
        require(lesson, "Урок не найден", 404)
        progress = course_progress(course_id, s)
        completed = progress["data"]["completed"]
        prerequisites(course, lesson, completed)
        if lesson_id not in completed:
            progress = update(
                g.db,
                progress,
                g.now,
                {**progress["data"], "completed": [*completed, lesson_id]},
            )
        return progress
