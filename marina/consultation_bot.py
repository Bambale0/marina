"""Telegram consultation checkout using server quotes and the shared email flow."""

from . import Problem
from .funnel import price_text


def quote_price(quote):
    return price_text(
        {"data": {"amount": quote["data"]["total"], "currency": quote["data"]["currency"]}}
    )


def show_quote(bot, booking, quote, refreshed=False):
    data = quote["data"]
    price = quote_price(quote)
    admin = bot.api("/me")["account"]["role"] == "admin"
    prefix = "Расчёт обновлён. Проверьте условия и подтвердите покупку.\n\n" if refreshed else ""
    text = (
        prefix
        + data["product_snapshot"]["title"]
        + "\n\n"
        + ("Стоимость тарифа: " if admin else "К оплате: ")
        + price
    )
    promotion = data.get("promotion_snapshot", {})
    if promotion.get("eligible") is True:
        text += (
            "\n\nСкидка по промокоду "
            + str(promotion.get("code", ""))
            + " закреплена за этой заявкой."
        )
        if promotion.get("terms"):
            text += "\n\nСохранённые условия скидки:\n" + promotion["terms"]
    else:
        text += (
            "\n\nК этой заявке применяется полная стоимость. "
            "Скидка по промокоду не была закреплена при записи."
        )
    if admin:
        text += "\nДля администратора доступ без оплаты."
    if booking["data"].get("payment_timing") == "before_scheduling":
        journey = booking["data"]["journey_snapshot"]
        text = (
            "🎯 Анкета принята! Ваши данные сохранены.\n\n"
            + (
                "Промокод " + str(promotion.get("code", "")) + " активирован.\n"
                if promotion.get("eligible") is True
                else ""
            )
            + "💳 К оплате: "
            + price
            + "\n\n"
            + journey["checkout_body"]
        )
    bot.send(
        text,
        [
            (
                "Открыть консультацию без оплаты"
                if admin
                else "💳 Оплатить консультацию за " + price,
                "consultation_buy",
                {"id": booking["id"], "quote_id": quote["id"]},
            ),
            ("❌ Назад к описанию", "funnel_consultation", {"id": booking["data"]["run_id"]}),
        ],
    )


def offer_payment(bot, booking):
    """Unavailable payment configuration must never undo appointment confirmation."""
    if not booking["data"].get("run_id") or booking["data"].get("paid_at"):
        return
    try:
        quote = bot.api("/funnel-bookings/" + booking["id"] + "/quote", "POST", {})
    except Problem:
        promotion = booking["data"].get("promotion", {})
        amount = promotion.get(
            "amount_rub" if promotion.get("eligible") is True else "regular_amount_rub"
        )
        text = ""
        if type(amount) is int and amount > 0:
            text = (
                "Цена по условиям вашей заявки: "
                + price_text({"data": {"amount": amount * 100, "currency": "RUB"}})
                + ".\n\n"
            )
        bot.send(
            text + "Оплата консультации пока недоступна. "
            "К подтверждённой записи можно вернуться в разделе «Мои обращения»."
        )
        return
    show_quote(bot, booking, quote)


def handle_action(bot, action, values):
    if action != "consultation_buy":
        return False
    booking = bot.api("/bookings/" + values["id"])
    if booking["data"].get("paid_at"):
        order = bot.api("/orders/" + booking["data"]["order_id"])
        bot.send(completed_text(order))
        return True
    quote = bot.api("/funnel-bookings/" + booking["id"] + "/quote", "POST", {})
    if quote["id"] != values.get("quote_id"):
        show_quote(bot, booking, quote, refreshed=True)
        return True
    order = bot.api(
        "/orders",
        "POST",
        {"quote_id": quote["id"], "key": "bot-consultation:" + quote["id"]},
    )
    if order["status"] == "paid":
        bot.send(completed_text(order))
        return True
    bot.state_set(step="funnel_checkout_email", funnel_order_id=order["id"])
    bot.send(
        "Укажите email для электронного чека Lava.top. "
        "Он используется только для оформления платежа."
    )
    return True


def completed_text(order):
    if (
        order["data"].get("booking_snapshot", {}).get("data", {}).get("payment_timing")
        == "before_scheduling"
    ):
        return (
            "Доступ к консультации открыт без оплаты. Согласуйте дату и время со специалистом."
            if order["data"].get("access_source") == "admin"
            else "Консультация оплачена. Специалист свяжется с вами для согласования даты и времени."
        )
    return (
        "Консультация доступна без оплаты. Время встречи сохранено в вашей записи."
        if order["data"].get("access_source") == "admin"
        else "Консультация оплачена. Время встречи сохранено в вашей записи."
    )
