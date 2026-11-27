"""T-Bank internet acquiring (eacq /v2), with signed notifications and GET-state reconciliation.

Retry creation only after authenticated CheckOrder returns order-not-found. Only a verified CONFIRMED payment grants rights.
https://developer.tbank.ru/eacq/intro/developer/token
"""

import hashlib
import hmac
import logging
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from . import Problem, object_get, require
from .commerce import checkout_open, confirm, provider_enabled
from .db import connect


def _scalar(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def sign(data, password):
    """Token includes only non-null scalar top-level fields and the terminal password."""
    fields = {
        key: value
        for key, value in data.items()
        if key not in ("Token", "Password")
        and value is not None
        and type(value) in (str, int, float, bool)
    }
    fields["Password"] = password
    return hashlib.sha256(
        "".join(_scalar(fields[key]) for key in sorted(fields)).encode("utf-8")
    ).hexdigest()


def verify_notification(app, data):
    token = data.get("Token")
    terminal = app.config.get("TBANK_TERMINAL_KEY", "")
    password = app.config.get("TBANK_PASSWORD", "")
    return bool(
        terminal
        and password
        and data.get("TerminalKey") == terminal
        and isinstance(token, str)
        and len(token) == 64
        and hmac.compare_digest(sign(data, password), token.lower())
    )


class TbankFailure(Problem):
    """Sanitized failure category; never include bank bodies or credential values."""

    def __init__(self, category, *, code=None, retryable=True):
        super().__init__("Счёт Т-Банка требует сверки", 503)
        self.category = category
        self.code = code
        self.retryable = retryable


def error_code(result):
    code = str(result.get("ErrorCode", "")) if isinstance(result, dict) else ""
    return code if re.fullmatch(r"-?[0-9]{1,6}", code) else None


def _api(app, path, payload):
    from .providers import remote

    terminal = app.config.get("TBANK_TERMINAL_KEY", "")
    password = app.config.get("TBANK_PASSWORD", "")
    require(terminal and password, "Эквайринг Т-Банка не настроен", 503)
    base = app.config.get("TBANK_API_URL", "https://securepay.tinkoff.ru")
    # Never let untrusted HTTP request data choose the payment API destination.
    require(
        base in ("https://securepay.tinkoff.ru", "https://rest-api-test.tinkoff.ru"),
        "Неверный адрес API эквайринга",
        503,
    )
    data = {"TerminalKey": terminal, **payload}
    data["Token"] = sign(data, password)
    timeout = max(1, min(20, int(app.config.get("TBANK_HTTP_TIMEOUT", 15))))
    order_id = payload.get("OrderId", "")
    correlation = order_id if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", str(order_id)) else "-"
    started = time.monotonic()
    logger = logging.getLogger(__name__)
    try:
        result = remote(app, "POST", base + "/v2/" + path, data, timeout=timeout)
    except (Problem, URLError, TimeoutError, ValueError) as exc:
        cause = exc.__cause__ or exc
        reason = getattr(cause, "reason", None)
        category = type(reason if isinstance(reason, BaseException) else cause).__name__
        status = cause.code if isinstance(cause, HTTPError) else None
        logger.warning(
            "tbank operation=%s order_id=%s elapsed_ms=%d failure=%s http_status=%s",
            path,
            correlation,
            int((time.monotonic() - started) * 1000),
            category,
            status,
        )
        raise TbankFailure("transport") from None
    code = error_code(result)
    success = isinstance(result, dict) and result.get("Success") is True
    logger.log(
        logging.INFO if success else logging.WARNING,
        "tbank operation=%s order_id=%s elapsed_ms=%d success=%s error_code=%s",
        path,
        correlation,
        int((time.monotonic() - started) * 1000),
        success,
        code,
    )
    return result


def _receipt(app, attempt):
    taxation = app.config.get("TBANK_RECEIPT_TAXATION", "")
    tax = app.config.get("TBANK_RECEIPT_TAX", "")
    obj = app.config.get("TBANK_RECEIPT_PAYMENT_OBJECT", "")
    if not (taxation or tax or obj):
        return None  # Fiscalization may be managed by the terminal; verify with bank.
    require(taxation and tax and obj, "Настройте все поля онлайн-кассы Т-Банка", 503)
    email = attempt["data"].get("email")
    require(isinstance(email, str) and "@" in email and len(email) <= 64, "Укажите email для чека")
    total = attempt["data"]["total"]
    return {
        "Email": email,
        "Taxation": taxation,
        "Items": [
            {
                "Name": attempt["data"]["title"][:128],
                "Price": total,
                "Quantity": 1,
                "Amount": total,
                "Tax": tax,
                "PaymentObject": obj,
                "PaymentMethod": "full_payment",
            }
        ],
    }


def init_invoice(app, attempt):
    data = attempt["data"]
    require(data.get("provider") == "tbank", "Неверный провайдер платежа")
    require(type(data.get("total")) is int and data["total"] > 0, "Неверная сумма счёта")
    require(data.get("currency") == "RUB", "Т-Банк принимает RUB")
    require(len(attempt["id"]) <= 50, "Неверный ID платежа")
    public = app.config.get("PUBLIC_URL", "").rstrip("/")
    parsed = urlsplit(public)
    require(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment,
        "Настройте публичный HTTPS URL",
        503,
    )
    path = data.get("return_path", "/")
    require(path in ("/", "/wallet"), "Неверная страница возврата")
    if path == "/wallet":
        from .neurocoins import configured_web_origin

        origin = configured_web_origin(app)
        require(
            app.config.get("NEUROCOINS_TBANK_ENABLED") is True and origin,
            "Веб-касса пополнения отключена",
            403,
        )
    else:
        origin = public
    payload = {
        "Amount": data["total"],
        "OrderId": attempt["id"],
        "Description": data["title"][:140],
        "NotificationURL": public + "/api/webhooks/tbank",
        "SuccessURL": origin + path + "?payment=success",
        "FailURL": origin + path + "?payment=failed",
    }
    receipt = _receipt(app, attempt)
    if receipt:
        payload["Receipt"] = receipt
    result = _api(app, "Init", payload)
    if not (
        isinstance(result, dict)
        and result.get("Success") is True
        and isinstance(result.get("PaymentId"), (str, int))
        and str(result["PaymentId"]).isdigit()
        and len(str(result["PaymentId"])) <= 20
        and isinstance(result.get("PaymentURL"), str)
        and urlsplit(result["PaymentURL"]).scheme == "https"
        and bool(urlsplit(result["PaymentURL"]).netloc)
    ):
        raise TbankFailure("init_response", code=error_code(result))
    return {"url": result["PaymentURL"], "provider_id": str(result["PaymentId"])}


def recover_invoice(app, attempt):
    """Never infer absence from a timeout, an empty successful list or a generic error."""
    if attempt["data"].get("provider_id"):
        raise TbankFailure("existing_payment_id", retryable=False)
    result = _api(app, "CheckOrder", {"OrderId": attempt["id"]})
    if (
        isinstance(result, dict)
        and result.get("Success") is False
        and error_code(result) == "335"
        and not result.get("Payments")
    ):
        with connect(app.config["DATABASE"]) as db:
            order = object_get(db, attempt["data"]["order_id"], "order", attempt["owner"])
            if not provider_enabled(app, "tbank") or not checkout_open(
                db, order, int(app.config["CLOCK"]())
            ):
                raise TbankFailure("checkout_closed", retryable=False)
            if order["data"].get("product_snapshot", {}).get("service") == "neurocoins" and (
                app.config.get("NEUROCOINS_TBANK_ENABLED") is not True
                or order["data"].get("surface") != "external_web"
            ):
                raise TbankFailure("checkout_closed", retryable=False)
        return init_invoice(app, attempt)
    if isinstance(result, dict) and result.get("Success") is True:
        # CheckOrder does not guarantee a reusable PaymentURL. Do not construct one.
        raise TbankFailure("existing_order", code=error_code(result), retryable=False)
    raise TbankFailure("check_order", code=error_code(result))


def check_state(app, db, attempt, now, payment_id=None):
    """Confirm only a bank-verified capture tied to the immutable attempt ID."""
    d = attempt["data"]
    require(d.get("provider") == "tbank", "Провайдер попытки не совпадает", 409)
    existing = d.get("provider_id")
    if existing and payment_id:
        require(str(existing) == str(payment_id), "Несовпадение ID платежа", 409)
    pid = str(payment_id or existing or "")
    require(pid.isdigit() and 1 <= len(pid) <= 20, "Нужна сверка ID платежа", 409)
    result = _api(app, "GetState", {"PaymentId": pid})
    require(isinstance(result, dict), "Т-Банк не вернул статус платежа", 503)
    state = result.get("Status", "unknown")
    if state != "CONFIRMED" or result.get("Success") is not True:
        return {"status": str(state)}
    require(
        str(result.get("PaymentId")) == pid
        and result.get("TerminalKey") == app.config["TBANK_TERMINAL_KEY"]
        and result.get("OrderId") == attempt["id"]
        and type(result.get("Amount")) is int
        and result["Amount"] == d["total"],
        "Платёж Т-Банка не соответствует счёту; требуется сверка",
        409,
    )
    order = object_get(db, d["order_id"], "order")
    return confirm(db, order, "tbank", pid, result["Amount"], "RUB", now, now)
