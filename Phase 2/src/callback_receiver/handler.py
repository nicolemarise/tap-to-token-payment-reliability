"""
POST /callbacks/payment - the "result URL" the payment gateway calls.

Callbacks can arrive twice, late, or never, so this endpoint is built to be
safe to call any number of times:

  1. Verify the signature (reject anything not from the gateway).
  2. Check the order exists and the amount matches.
  3. CLAIM the gateway reference with a conditional write. If it was already
     claimed, this is a duplicate: acknowledge it and do nothing.
  4. Move the order PENDING -> PAID (guarded by the state machine).
  5. Queue the order for token delivery.

If anything fails unexpectedly after the claim, the claim is released so the
gateway's retry can be processed. If we die between steps 4 and 5, the
reconciler finds the order stuck in PAID and queues it.
"""

import json
import time

from botocore.exceptions import ClientError

from ttt import common, states

CLAIM_TTL_SECONDS = 7 * 24 * 3600


def _process(order_id, reference, status):
    try:
        if status == "Paid":
            states.transition(
                order_id,
                states.PENDING,
                states.PAID,
                "gateway-callback",
                f"Payment confirmed by gateway ({reference})",
                {"gateway_reference": reference},
            )
        else:
            states.transition(
                order_id,
                states.PENDING,
                states.FAILED,
                "gateway-callback",
                f"Payment cancelled at gateway ({reference})",
            )
            return common.json_response(200, {"result": "cancelled"})
    except states.StaleState:
        # The order already moved on (for example the reconciler got there
        # first). If it is still waiting for delivery, make sure it is queued.
        current = states.get_order(order_id)
        if status == "Paid" and current and current["status"] == states.PAID:
            common.enqueue_delivery(order_id)
            return common.json_response(200, {"result": "requeued"})
        return common.json_response(200, {"result": "already_processed"})

    common.enqueue_delivery(order_id)
    return common.json_response(200, {"result": "accepted"})


def handler(event, context):
    body = common.raw_body(event)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if not common.verify(body, headers.get("x-gateway-signature", ""), common.get_secret()):
        return common.json_response(401, {"error": "Invalid signature"})

    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return common.json_response(400, {"error": "Body must be a JSON object"})

    reference = str(data.get("gateway_reference", ""))
    order_id = str(data.get("order_id", ""))
    amount = data.get("amount_cents")
    status = data.get("status")

    if not reference or not common.ORDER_ID_RE.match(order_id):
        return common.json_response(400, {"error": "Missing gateway_reference or order_id"})
    if status not in ("Paid", "Cancelled"):
        return common.json_response(400, {"error": "status must be Paid or Cancelled"})
    if not isinstance(amount, int) or isinstance(amount, bool):
        return common.json_response(400, {"error": "amount_cents must be an integer"})

    order = states.get_order(order_id)
    if not order:
        return common.json_response(404, {"error": "Unknown order"})
    if int(order["amount_cents"]) != amount:
        return common.json_response(422, {"error": "Amount does not match the order"})

    processed = common.table("PROCESSED_TABLE")
    now = int(time.time())
    try:
        processed.put_item(
            Item={
                "gateway_reference": reference,
                "order_id": order_id,
                "received_at": now,
                "expires_at": now + CLAIM_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(gateway_reference)",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return common.json_response(200, {"result": "duplicate"})
        raise

    try:
        return _process(order_id, reference, status)
    except Exception:
        processed.delete_item(Key={"gateway_reference": reference})
        raise
