"""
Reads paid orders from the delivery queue and asks the utility supplier for
the token.

Safe to run repeatedly for the same order:
  - It only acts on orders that are still PAID.
  - The supplier is idempotent on the reference (we send the order_id), so a
    retry after a timeout returns the same token and never a second one.

If the supplier fails or times out, the message goes back on the queue and is
retried. After the retries run out it lands in the dead-letter queue, and the
reconciler takes over.
"""

import json
import os

from ttt import common, states


def _deliver(order_id):
    order = states.get_order(order_id)
    if not order or order["status"] != states.PAID:
        return  # already delivered, refunded or unknown: nothing to do

    payload = json.dumps(
        {
            "reference": order_id,
            "meter_number": order["meter_number"],
            "amount_cents": int(order["amount_cents"]),
        }
    )
    status, data = common.http_request(
        "POST",
        f"{os.environ['UTILITY_URL']}/utility/token",
        payload,
        {"Content-Type": "application/json"},
        timeout=3,
    )
    if status != 200 or not isinstance(data, dict) or "token" not in data:
        raise RuntimeError(f"Supplier returned {status}")

    try:
        states.transition(
            order_id,
            states.PAID,
            states.TOKEN_ISSUED,
            "delivery-worker",
            "Token issued by supplier",
            {"token": data["token"]},
        )
    except states.StaleState:
        pass  # someone else (e.g. the reconciler) already completed it


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            _deliver(json.loads(record["body"])["order_id"])
        except Exception as error:  # noqa: BLE001 - any failure means "retry later"
            print(f"delivery failed, will retry: {error}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
