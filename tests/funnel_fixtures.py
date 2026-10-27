"""Synthetic copy only. The owner's paid guides must never be checked into Git."""

import json

from marina.funnel import AGE_MONTHS, AGES


def configure_funnel_source(app, tmp_path):
    groups = {}
    filenames = {
        "3-4": "Neiro_Aptechka_3-4_goda.pdf",
        "5-6": "Neiro_Podgotovka_1_klass.pdf",
        "7-9": "Neiro_Shkola_7-9_let.pdf",
        "10-14": "Dofaminovaya_Perezagruzka_10-14.pdf",
    }
    for group, label in AGES.items():
        questions = [
            {
                "id": f"q{i}",
                "text": f"[Вопрос {i} из 6] Синтетический вопрос {group}",
                "required": True,
                "options": [
                    {"id": letter, "text": "Тестовый вариант " + letter, "score": score}
                    for score, letter in enumerate("abc")
                ],
            }
            for i in range(1, 7)
        ]
        bands = [
            {
                "min": lo,
                "max": hi,
                "zone": zone,
                "text": "📊 ИТОГ СКРИНИНГА: {score} из 12 баллов. Тестовая зона " + zone,
            }
            for lo, hi, zone in [(0, 3, "green"), (4, 7, "yellow"), (8, 12, "red")]
        ]
        method = {
            "audience": "child",
            "min_months": AGE_MONTHS[group][0],
            "max_months": AGE_MONTHS[group][1],
            "source": "SYNTHETIC ENGINEERING FIXTURE, NOT A CLINICAL METHOD",
            "rights": "Synthetic fixture",
            "limitations": "Не медицинский диагноз. Синтетический тест.",
            "questions": questions,
            "bands": bands,
            "examples": [
                {
                    "answers": {q["id"]: value for q in questions},
                    "score": score,
                    "text": bands[index]["text"],
                }
                for value, score, index in [("a", 0, 0), ("b", 6, 1), ("c", 12, 2)]
            ],
        }
        groups[group] = {
            "title": "Нейро-скрининг: " + label.split(" ", 1)[1],
            "age_group": group,
            "method": method,
            "offer_text": "Тестовое руководство. Стоимость: {price}. Срок предложения — 2 часа.",
            "tripwire_delay_seconds": 180,
            "guide_title": "Synthetic guide " + group,
            "guide_pages": [
                {
                    "title": "Тестовая страница " + str(i),
                    "text": "Синтетический материал для проверки PDF. Не содержит рекомендаций.",
                }
                for i in range(1, 15)
            ],
            "guide_filename": filenames[group],
            "guide_product_id": "",
            "free_material_id": "",
            "refusal_material_id": "",
            "channel_url": "",
            "contact_url": "",
            "consultation_text": "Тестовая консультация. Промокод TEST_PROMO.",
            "consultation_intro_text": "Тестовая консультация, без рекомендаций.",
            "consultation_valid_days": 7,
            "consultation_code": "TEST_PROMO",
            "consultation_amount_rub": 10,
            "consultation_regular_rub": 20,
            "success_text": "Оплата прошла успешно. Тестовый файл готов.",
            "cart_first_text": "{name}, вы не завершили оплату тестового руководства.",
            "cart_last_text": "{name}, предложение закончится через 15 минут.",
            "refusal_text": "Предложение закрыто. Тестовый результат сохранён.",
            "followups": [
                {
                    "hours": hours,
                    "text": "Тестовое касание "
                    + str(hours)
                    + ". Промокод TEST_PROMO действует до {deadline}.",
                }
                for hours in (24, 48, 72)
            ],
            "timezone": "Europe/Moscow",
            "price_rub": 1,
            "buyer_tag": "buyer_guide_" + group.replace("-", "_"),
            "refused_tag": "refused_tripwire_" + group.replace("-", "_"),
        }
    path = tmp_path / "private-synthetic-scenario.json"
    path.write_text(json.dumps(groups, ensure_ascii=False))
    app.config["FUNNEL_SOURCE_PATH"] = str(path)
