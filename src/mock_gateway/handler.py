"""
MOCK payment gateway. This stands in for a real gateway such as Paynow.
It is a simulation: no real money, no real provider.

Routes:
  POST /gateway/pay               the customer pays for an order
  GET  /gateway/status/{order_id} "what happened to this payment?" (reconciler uses this)
  POST /gateway/refund            refund a payment (idempotent)
  GET  /chaos                     read the chaos switch
  PUT  /chaos                     change the chaos switch

When a payment is taken, the gateway sends a "payment confirmed" callback to
our system. The callback goes through an outbox queue so we can misbehave
realistically, driven by the chaos switch:
  - duplicate: the same callback is sent twice
  - delayed:   the callback arrives after a delay (up to 15 minutes)
  - lost:      the callback is never sent
"""

import json
import os
import random
import time
import uuid

from botocore.exceptions import ClientError

from ttt import common

FORCE_VALUES = (None, "none", "duplicate", "delayed", "lost")


def _is_conditional_failure(error):
    return error.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _decide(force, chaos):
    """Return (lost, delayed, duplicate) for this payment."""
    if force == "lost":
        return True, False, False
    if force == "delayed":
        return False, True, False
    if force == "duplicate":
        return False, False, True
    if force == "none":
        return False, False, False
    if random.random() < chaos["lost_callback_rate"]:
        return True, False, False
    delayed = random.random() < chaos["delayed_callback_rate"]
    duplicate = random.random() < chaos["duplicate_callback_rate"]
    return False, delayed, duplicate


def _pay(event):
    data = common.parse_body(event)
    order_id = str(data.get("order_id", ""))
    if not common.ORDER_ID_RE.match(order_id):
        raise ValueError("order_id is missing or invalid")
    amount = data.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amount_cents must be a positive integer")
    force = data.get("force")
    if force not in FORCE_VALUES:
        raise ValueError("force must be one of: none, duplicate, delayed, lost")

    payments = common.table("GATEWAY_TABLE")
    reference = "gw-" + uuid.uuid4().hex[:16]
    try:
        payments.put_item(
            Item={
                "order_id": order_id,
                "gateway_reference": reference,
                "amount_cents": amount,
                "status": "PAID",
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(order_id)",
        )
    except ClientError as error:
        if not _is_conditional_failure(error):
            raise
        # The gateway also protects against paying twice for the same order.
        existing = payments.get_item(Key={"order_id": order_id})["Item"]
        return common.json_response(
            200,
            {"gateway_reference": existing["gateway_reference"], "already_paid": True},
        )

    chaos = common.get_chaos()
    lost, delayed, duplicate = _decide(force, chaos)

    outcome = "sent"
    if lost:
        outcome = "lost"
    else:
        message = json.dumps(
            {
                "gateway_reference": reference,
                "order_id": order_id,
                "amount_cents": amount,
                "status": "Paid",
            }
        )
        delay = int(min(max(chaos["delayed_seconds"], 0), 900)) if delayed else 0
        copies = 2 if duplicate else 1
        for _ in range(copies):
            common.client("sqs").send_message(
                QueueUrl=os.environ["OUTBOX_QUEUE_URL"],
                MessageBody=message,
                DelaySeconds=delay,
            )
        if duplicate:
            outcome = "duplicated"
        elif delayed:
            outcome = "delayed"

    return common.json_response(
        200,
        {
            "gateway_reference": reference,
            "already_paid": False,
            "callback": outcome,
        },
    )


def _status(event):
    order_id = (event.get("pathParameters") or {}).get("order_id", "")
    item = common.table("GATEWAY_TABLE").get_item(Key={"order_id": order_id}).get("Item")
    if not item:
        return common.json_response(404, {"status": "NOT_FOUND"})
    return common.json_response(
        200,
        {
            "status": item["status"],
            "gateway_reference": item["gateway_reference"],
            "amount_cents": item["amount_cents"],
        },
    )


def _refund(event):
    data = common.parse_body(event)
    order_id = str(data.get("order_id", ""))
    if not common.ORDER_ID_RE.match(order_id):
        raise ValueError("order_id is missing or invalid")

    payments = common.table("GATEWAY_TABLE")
    try:
        payments.update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :refunded, refunded_at = :now",
            ConditionExpression="#s = :paid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":refunded": "REFUNDED",
                ":paid": "PAID",
                ":now": int(time.time()),
            },
        )
        return common.json_response(200, {"refunded": True, "already_refunded": False})
    except ClientError as error:
        if not _is_conditional_failure(error):
            raise
        item = payments.get_item(Key={"order_id": order_id}).get("Item")
        if not item:
            return common.json_response(404, {"error": "No payment found for this order"})
        if item["status"] == "REFUNDED":
            return common.json_response(200, {"refunded": True, "already_refunded": True})
        return common.json_response(409, {"error": f"Payment is {item['status']}"})


def _set_chaos(event):
    data = common.parse_body(event)
    chaos = common.get_chaos()
    for key, value in data.items():
        if key not in common.CHAOS_DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        upper = 900 if key == "delayed_seconds" else 1
        if not 0 <= value <= upper:
            raise ValueError(f"{key} must be between 0 and {upper}")
        chaos[key] = float(value)
    common.set_chaos(chaos)
    return common.json_response(200, chaos)


def handler(event, context):
    route = event.get("routeKey", "")
    try:
        if route == "POST /gateway/pay":
            return _pay(event)
        if route == "GET /gateway/status/{order_id}":
            return _status(event)
        if route == "POST /gateway/refund":
            return _refund(event)
        if route == "GET /chaos":
            return common.json_response(200, common.get_chaos())
        if route == "PUT /chaos":
            return _set_chaos(event)
    except ValueError as error:
        return common.json_response(400, {"error": str(error)})
    return common.json_response(404, {"error": f"Unknown route: {route}"})
