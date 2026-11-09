"""
POST /orders  - create a token purchase.

The client MUST send an `Idempotency-Key` header (a unique ID generated once
per purchase attempt, e.g. a UUID). That key becomes the order_id.

  - First request with a key  -> 201, order created in PENDING
  - Same key again (double tap, refresh, network retry) -> 200, the ORIGINAL
    order is returned and nothing new is created
  - Same key but a different meter/amount -> 409 (the key was reused wrongly)

Body (JSON):
    {"meter_number": "01234567890", "amount_cents": 1000}
"""

import json
import re
import time
from decimal import Decimal

from ttt import states

ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
METER_RE = re.compile(r"^\d{8,13}$")
MIN_CENTS = 100  # $1.00
MAX_CENTS = 50000  # $500.00


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Cannot serialise {type(value)}")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _public_view(order):
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "meter_number": order["meter_number"],
        "amount_cents": order["amount_cents"],
        "currency": order["currency"],
        "created_at": order["created_at"],
    }


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    order_id = headers.get("idempotency-key", "")
    if not ORDER_ID_RE.match(order_id):
        return _response(
            400,
            {
                "error": "Missing or invalid Idempotency-Key header "
                "(8-64 characters: letters, digits, - and _)"
            },
        )

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body must be valid JSON"})

    meter_number = str(payload.get("meter_number", ""))
    amount_cents = payload.get("amount_cents")

    if not METER_RE.match(meter_number):
        return _response(400, {"error": "meter_number must be 8-13 digits"})
    if (
        not isinstance(amount_cents, int)
        or isinstance(amount_cents, bool)
        or not MIN_CENTS <= amount_cents <= MAX_CENTS
    ):
        return _response(
            400,
            {"error": f"amount_cents must be an integer {MIN_CENTS}-{MAX_CENTS}"},
        )

    now = int(time.time())
    order = {
        "order_id": order_id,
        "status": states.PENDING,
        "meter_number": meter_number,
        "amount_cents": amount_cents,
        "currency": "USD",
        "created_at": now,
        "updated_at": now,
    }

    try:
        states.create_order(order)
    except states.OrderAlreadyExists:
        existing = states.get_order(order_id)
        if (
            existing["meter_number"] != meter_number
            or int(existing["amount_cents"]) != amount_cents
        ):
            return _response(
                409,
                {"error": "Idempotency-Key was already used for a different request"},
            )
        return _response(
            200, {"order": _public_view(existing), "idempotent_replay": True}
        )

    return _response(201, {"order": _public_view(order), "idempotent_replay": False})
