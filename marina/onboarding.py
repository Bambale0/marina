"""Owner-supplied introduction copy, separate from screening methods and scores."""

from . import require

AGE_GROUPS = {
    "3-4": ("3–4 года", 36, 59),
    "5-6": ("5–6 лет", 60, 83),
    "7-8": ("7–8 лет", 84, 107),
    "9-10": ("9–10 лет", 108, 131),
}

QUESTIONS = {
    "3-4": [
        "Ребёнок мало говорит?",
        "Не понимает, что Вы просите?",
        "Быстро отвлекается во время игры?",
        "Часто расстраивается и долго успокаивается?",
        "Ему трудно бегать, прыгать, ловить мяч?",
    ],
    "5-6": [
        "Сомневаетесь, готов ли ребёнок к школе?",
        "Отвлекается, не закончив задание?",
        "Забывает, что нужно сделать?",
        "Ему трудно рассказывать и пересказывать?",
        "Трудно держать карандаш или пользоваться ножницами?",
    ],
    "7-8": [
        "Тяжело привыкает к школе?",
        "Чтение даётся с трудом?",
        "Часто ошибается при письме?",
        "Путается в счёте?",
        "Быстро отвлекается и устаёт на уроках?",
        "Ему трудно понять и запомнить новую тему?",
        "Сильно переживает из-за учёбы и ошибок?",
    ],
    "9-10": [
        "Быстро отвлекается и устаёт на уроках?",
        "Без Вас уроки не делает?",
        "Ему трудно понять и запомнить новую тему?",
        "Ошибки в чтении или письме повторяются?",
        "Не знает, с чего начать, и бросает дела?",
        "Сильно переживает из-за учёбы и ошибок?",
    ],
}


def screen(db, age_group):
    require(age_group in AGE_GROUPS, "Выберите возрастную группу")
    from .db import unpack

    row = db.execute(
        "SELECT * FROM objects WHERE kind='onboarding' AND status='published' "
        "AND json_extract(data,'$.age_group')=? ORDER BY created DESC,rowid DESC LIMIT 1",
        (age_group,),
    ).fetchone()
    if row:
        obj = unpack(row)
        return obj["data"]["concerns"]
    return [{"id": str(i), "text": text} for i, text in enumerate(QUESTIONS[age_group], 1)]


def validate_screen(data):
    require(data.get("age_group") in AGE_GROUPS, "Выберите группу: 3-4, 5-6, 7-8 или 9-10")
    items = data.get("concerns")
    require(isinstance(items, list) and 1 <= len(items) <= 12, "Добавьте от 1 до 12 вопросов")
    seen = set()
    for item in items:
        require(isinstance(item, dict), "Неверный вопрос")
        id, text = item.get("id"), item.get("text")
        require(
            isinstance(id, str) and 1 <= len(id) <= 40 and id not in seen,
            "Укажите уникальный ID каждого вопроса (до 40 символов)",
        )
        require(
            isinstance(text, str) and 1 <= len(text.strip()) <= 200,
            "Нужен текст вопроса до 200 символов",
        )
        seen.add(id)
