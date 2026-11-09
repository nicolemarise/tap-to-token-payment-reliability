"""
MOCK utility supplier. This stands in for a real electricity-token supplier.
It is a simulation: the tokens it generates are random and worth nothing.

Routes:
  POST /utility/token               issue a token for a reference
  GET  /utility/status/{reference}  "did you issue a token for this reference?"

Two behaviours matter:

1. IDEMPOTENT BY REFERENCE. Asking for a token twice with the same reference
   returns the SAME token. That makes it safe for our system to retry after a
   timeout: a retry can never produce a second token.

2. THE TIMEOUT PROBLEM (chaos switch: provider_timeout_rate). When triggered,
   the supplier is slow enough that the caller gives up, and half the time it
   HAS issued the token before going quiet. The caller cannot tell the
   difference. That is exactly the "paid, but is the token real?" situation
   the reconciler exists to resolve.
"""

import os
import random
import secrets
import time

from botocore.exceptions import ClientError

from ttt import common


def _new_token():
    digits = "".join(secrets.choice("0123456789") for _ in range(20))
    return "-".join(digits[i : i + 4] for i in range(0, 20, 4))


def _issue(reference, meter_number, amount_cents):
    """Issue a token once per reference; return the existing one on repeats."""
    tokens = common.table("SUPPLIER_TABLE")
    token = _new_token()
    try:
        tokens.put_item(
            Item={
                "reference": reference,
                "token": token,
                "meter_number": meter_number,
                "amount_cents": amount_cents,
                "issued_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(#ref)",
            ExpressionAttributeNames={"#ref": "reference"},
        )
        return token
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return tokens.get_item(Key={"reference": reference})["Item"]["token"]


def _token(event):
    data = common.parse_body(event)
    reference = str(data.get("reference", ""))
    meter_number = str(data.get("meter_number", ""))
    amount_cents = data.get("amount_cents")
    if not common.ORDER_ID_RE.match(reference):
        raise ValueError("reference is missing or invalid")
    if not meter_number.isdigit():
        raise ValueError("meter_number must be digits")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
        raise ValueError("amount_cents must be an integer")

    chaos = common.get_chaos()
    if random.random() < chaos["provider_timeout_rate"]:
        if random.random() < 0.5:
            _issue(reference, meter_number, amount_cents)  # processed, reply lost
        time.sleep(float(os.environ.get("SLOW_SECONDS", "6")))
        return common.json_response(504, {"error": "Supplier timed out"})

    token = _issue(reference, meter_number, amount_cents)
    return common.json_response(200, {"reference": reference, "token": token})


def _status(event):
    reference = (event.get("pathParameters") or {}).get("reference", "")
    item = common.table("SUPPLIER_TABLE").get_item(Key={"reference": reference}).get("Item")
    if not item:
        return common.json_response(200, {"issued": False})
    return common.json_response(200, {"issued": True, "token": item["token"]})


def handler(event, context):
    route = event.get("routeKey", "")
    try:
        if route == "POST /utility/token":
            return _token(event)
        if route == "GET /utility/status/{reference}":
            return _status(event)
    except ValueError as error:
        return common.json_response(400, {"error": str(error)})
    return common.json_response(404, {"error": f"Unknown route: {route}"})

