from pathlib import Path
import argparse
import re
import json
import hashlib

p = argparse.ArgumentParser()
p.add_argument("--text", required=True)
p.add_argument("--pdf", required=True)
p.add_argument("--output", required=True)
args = p.parse_args()
s = Path(args.text).read_text()
starts = [m.start() for m in re.finditer(r"ТИП 1\.", s)]
outside = s.index("Кнопка «Другой возраст»")
assert len(starts) == 4
config = {
    "source": {
        "file": "ДЛЯ БОТА ОСТАТОК.pdf",
        "sha256": hashlib.sha256(Path(args.pdf).read_bytes()).hexdigest(),
        "pasted_sha256": hashlib.sha256(Path(args.text).read_bytes()).hexdigest(),
    },
    "ages": {},
}
for group, b in zip(
    ("3-4", "5-6", "7-9", "10-14"), [s[a:b] for a, b in zip(starts, starts[1:] + [outside])]
):
    desc = b.split("Сообщение бота:\n", 1)[1].split("💳 Стоимость", 1)[0].strip()
    questions = []
    for n in (1, 2, 3):
        begin = b.index("«Шаг " + str(n) + " из 3:")
        end = b.index("»", begin)
        prompt = b[begin + 1 : end].strip()
        following = b[
            end + 1 : b.index("«Шаг " + str(n + 1) + " из 3:") if n < 3 else b.index("ШАГ 2:")
        ]
        options = [
            {
                "id": "other" if "Свой вариант" in label else "option_" + str(i + 1),
                "label": label.rstrip("»"),
            }
            for i, label in enumerate(re.findall(r"^\[Кнопка\]: (.+)$", following, re.M))
        ]
        if n == 1:
            options = []
        questions.append(
            {
                "id": ("profile", "problem", "slot")[n - 1],
                "prompt": prompt,
                "options": options,
                "allow_custom": n == 2,
            }
        )
        if n == 2:
            assert len(options) in (4, 5, 6), (group, options)
        if n == 3:
            assert len(options) == 3, (group, options)
    step2 = b.index("ШАГ 2:")
    payment = b[step2:]
    body_start = min(
        x
        for x in (payment.find("Как мы"), payment.find("Как всё"), payment.find("Как строится"))
        if x >= 0
    )
    body = payment[body_start : payment.index("[Кнопка]: 💳")].strip()
    success = b[b.index("🎉", step2) : b.index("\nCode", step2)].strip()
    filename = re.search(r"([^\n]+\.pdf)", success)[1]
    before, after = success.split(filename, 1)
    before = before.replace("3 900 ₽", "{price}").replace("4 500 ₽", "{price}")
    contact = b[b.index("ТИП 3.") :]
    contact = (
        contact[contact.index("«{имя}") + 1 : contact.index("[Кнопка]: 👩")].strip().rstrip("«»*")
    )
    prefill = (
        b.split("https://t.me/ваш_юзернейм?text=", 1)[1]
        .split("\nДля возраст", 1)[0]
        .split("\nЭто на возраст", 1)[0]
        .strip()
    )
    config["ages"][group] = {
        "version": "remainder-20261105",
        "description": desc,
        "questions": questions,
        "checkout_body": body,
        "success_before_file": before.strip(),
        "success_after_file": after.strip(),
        "preparation_filename": filename,
        "preparation_material_id": "",
        "contact_text": contact.replace("{имя}", "{name}"),
        "contact_prefill": prefill,
        "payment_timing": "after_scheduling",
        "requested_payment_timing": "before_scheduling",
    }
out = s[outside:]
menu = out.split("Сообщение бота:\n", 1)[1].split("[Кнопка]:", 1)[0].strip()
config["outside"] = {
    "title": "Маршруты другого возраста",
    "type": "consultation_routes",
    "terms": "Авторские сценарии из ДЛЯ БОТА ОСТАТОК.pdf; личная связь без автоматического детского скрининга.",
    "menu_text": menu,
    "contact_url": "https://t.me/Marina_neiropsiholog",
    "specialist_name": "Марина",
    "routes": {},
}
for key, label, price, part in zip(
    ("baby_0_2", "teen_senior", "adult"),
    (
        "🍼 Малыш до 3 лет (раннее развитие)",
        "🎓 Подросток 15–18 лет (экзамены, тревога)",
        "🧠 Ищу для себя (Взрослый: выгорание / фокус)",
    ),
    (1900, 4500, 5000),
    re.split(r"ВЕТКА [123]:", out)[1:],
):
    text = part.split("1. Сообщение бота:\n", 1)[1].split("[Кнопка]:", 1)[0].strip()
    if key == "baby_0_2":
        intro, gift = text.split("🎁 Бесплатная польза", 1)
        gift, ending = gift.split("💡 Как получить", 1)
        text = intro.strip() + "\n\n💡 Как получить" + ending
    else:
        gift = ""
    text = text.replace("4 500 ₽", "{price}").replace("5 000 ₽", "{price}")
    config["outside"]["routes"][key] = {
        "label": label,
        "text": text,
        "amount_rub": price,
        "booking_label": re.search(r"\[Кнопка\]: (.+)", part)[1].replace("1 900 ₽", "{price}"),
        "gift_text": ("🎁 Бесплатная польза" + gift)
        .replace("[Файл чек-листа прикрепляется прямо в чат]", "")
        .strip()
        if gift
        else "",
        "material_id": "",
        "material_filename": "Чек-лист: Моторные вехи ребенка от 0 до 3 лет (Сенсорный календарь).pdf"
        if gift
        else "",
    }
config["outside"]["routes"]["baby_0_2"].update(
    required_videos=3, max_video_seconds=60, response_hours=24
)
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
print("Four author journeys and three routes extracted")
