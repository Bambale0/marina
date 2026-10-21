"""Declarative, bounded scoring; no eval or generated clinical content."""

import hashlib
import json

from flask import g
from . import age_months, body, object_get, protected, require
from .db import audit, insert, unpack, update


def score(d, answers):
    require(isinstance(answers, dict), "Неверные ответы")
    known = {q["id"] for q in d["questions"]}
    require(set(answers) <= known, "Неизвестный вопрос")
    total = 0
    active = []
    for q in d["questions"]:
        condition = q.get("when")
        if condition and answers.get(condition["question"]) != condition["answer"]:
            require(q["id"] not in answers, "Ответ на скрытый вопрос")
            continue
        active.append(q["id"])
        if q["id"] not in answers:
            require(not q.get("required", True), "Ответьте на обязательные вопросы")
            continue
        choices = {o["id"]: o["score"] for o in q["options"]}
        require(
            isinstance(answers[q["id"]], str) and answers[q["id"]] in choices,
            "Недопустимый вариант ответа",
        )
        total += choices[answers[q["id"]]]
    bands = [b for b in d["bands"] if b["min"] <= total <= b["max"]]
    require(len(bands) == 1, "Результат вне утверждённых диапазонов")
    return {
        "score": total,
        "text": bands[0]["text"],
        "limitations": d["limitations"],
        "active_questions": active,
    }


def validate_method(d):
    require(
        d.get("audience") in ("adult", "child")
        and type(d.get("min_months")) is int
        and type(d.get("max_months")) is int
        and 0 <= d["min_months"] <= d["max_months"] <= 1440,
        "Укажите аудиторию и возраст в месяцах",
    )
    require(
        all(
            isinstance(d.get(k), str) and d[k].strip() for k in ("source", "rights", "limitations")
        ),
        "Нужны источник, права использования и ограничения вывода",
    )
    require(
        isinstance(d.get("questions"), list) and 1 <= len(d["questions"]) <= 200,
        "Нужны вопросы (не более 200)",
    )
    seen = {}
    for q in d["questions"]:
        require(
            isinstance(q, dict)
            and isinstance(q.get("id"), str)
            and q["id"]
            and q["id"] not in seen
            and q.get("text"),
            "Неверный или повторяющийся вопрос",
        )
        require(
            isinstance(q.get("options"), list) and 1 <= len(q["options"]) <= 30,
            "Нужны варианты ответа",
        )
        choices = set()
        for o in q["options"]:
            require(
                isinstance(o, dict)
                and isinstance(o.get("id"), str)
                and o["id"] not in choices
                and o.get("text")
                and type(o.get("score")) is int
                and abs(o["score"]) <= 10000,
                "Неверные варианты или баллы",
            )
            choices.add(o["id"])
        if q.get("when"):
            w = q["when"]
            require(
                isinstance(w, dict)
                and w.get("question") in seen
                and w.get("answer") in seen[w["question"]],
                "Ветвление должно ссылаться на предыдущий вопрос и его вариант",
            )
        seen[q["id"]] = choices
    require(
        isinstance(d.get("bands"), list) and 1 <= len(d["bands"]) <= 100,
        "Нужны диапазоны интерпретаций",
    )
    previous = None
    for b in sorted(d["bands"], key=lambda x: x.get("min", 0)):
        require(
            type(b.get("min")) is int
            and type(b.get("max")) is int
            and b["min"] <= b["max"]
            and isinstance(b.get("text"), str)
            and b["text"],
            "Неверный диапазон",
        )
        require(previous is None or b["min"] > previous, "Диапазоны пересекаются")
        previous = b["max"]
    require(
        isinstance(d.get("examples"), list) and len(d["examples"]) >= 2,
        "Нужны контрольные примеры специалиста",
    )
    for ex in d["examples"]:
        actual = score(d, ex.get("answers"))
        require(
            actual["score"] == ex.get("score") and actual["text"] == ex.get("text"),
            "Контрольный пример не совпал с расчётом",
        )


def idempotent(db, scope, key, payload, create):
    require(isinstance(key, str) and 1 <= len(key) <= 120, "Нужен ключ повторного запроса")
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    previous = db.execute("SELECT * FROM dedup WHERE scope=? AND key=?", (scope, key)).fetchone()
    if previous:
        require(
            previous["fingerprint"] == fingerprint,
            "Ключ уже использован для другого запроса",
            409,
        )
        return object_get(db, previous["object_id"])
    obj = create()
    db.execute("INSERT INTO dedup VALUES(?,?,?,?)", (scope, key, obj["id"], fingerprint))
    return obj


def register_screening(app):
    @app.post("/api/attempts")
    @protected()
    def start_attempt():
        d = body()
        subject = object_get(g.db, d.get("subject_id"), "subject", g.actor["id"])
        method = object_get(g.db, d.get("method_id"), "method")

        def start():
            require(
                subject["status"] == "active" and method["status"] == "published",
                "Методика или профиль недоступны",
            )
            m = method["data"]
            require(
                subject["data"]["kind"] == m["audience"]
                and m["min_months"]
                <= age_months(subject["data"]["birth_date"], g.now)
                <= m["max_months"],
                "Методика не подходит по возрасту или аудитории",
            )
            return insert(
                g.db,
                "attempt",
                {
                    "method_id": method["id"],
                    "method_snapshot": m,
                    "subject_snapshot": subject["data"],
                    "answers": {},
                },
                g.now,
                g.actor["id"],
                subject["id"],
            )

        return idempotent(
            g.db,
            "attempt:" + g.actor["id"],
            d.get("key"),
            {k: d.get(k) for k in ("subject_id", "method_id")},
            start,
        )

    @app.get("/api/attempts/<id>")
    @protected()
    def read_attempt(id):
        return object_get(g.db, id, "attempt", g.actor["id"])

    @app.patch("/api/attempts/<id>")
    @protected()
    def save_answers(id):
        obj = object_get(g.db, id, "attempt", g.actor["id"])
        d = body()
        require(
            obj["status"] == "draft" and d.get("revision") == obj["revision"],
            "Ответы изменены или попытка завершена",
            409,
        )
        answers = d.get("answers")
        require(isinstance(answers, dict), "Неверные ответы")
        method = obj["data"]["method_snapshot"]
        questions = {q["id"]: q for q in method["questions"]}
        require(set(answers) <= set(questions), "Неизвестный вопрос")
        for key, value in answers.items():
            require(
                isinstance(value, str) and value in {o["id"] for o in questions[key]["options"]},
                "Недопустимый ответ",
            )
        return update(g.db, obj, g.now, {**obj["data"], "answers": answers})

    @app.post("/api/attempts/<id>/complete")
    @protected()
    def complete_attempt(id):
        obj = object_get(g.db, id, "attempt", g.actor["id"])
        if obj["status"] == "completed":
            return obj
        require(body().get("revision") == obj["revision"], "Ответы изменены", 409)
        result = score(obj["data"]["method_snapshot"], obj["data"]["answers"])
        result = update(
            g.db,
            obj,
            g.now,
            {**obj["data"], "result": result, "completed_at": g.now},
            "completed",
        )
        audit(
            g.db,
            g.actor["id"],
            "attempt.complete",
            result,
            g.now,
            "Завершение анкеты",
            obj["revision"],
        )
        return result

    @app.get("/api/history")
    @protected()
    def history():
        from .views import history_view

        return {
            "items": [
                history_view(unpack(r))
                for r in g.db.execute(
                    "SELECT * FROM objects WHERE owner=? AND kind IN ('attempt','order','quote','right','report','case','booking','lesson','payment_attempt','progress','plan') ORDER BY created DESC,id LIMIT 200",
                    (g.actor["id"],),
                )
            ]
        }
