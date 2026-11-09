"""
GET /orders/{order_id}

Read-only view of one order and its audit log, for the demo page.
Only fields that are safe to show are returned, and the meter number is
masked. Nothing here changes any data.
"""

from ttt import common, states

ORDER_FIELDS = (
    "order_id",
    "status",
    "amount_cents",
    "currency",
    "token",
    "created_at",
    "updated_at",
)


def _mask(meter_number):
    meter_number = str(meter_number or "")
    return "*" * max(len(meter_number) - 4, 0) + meter_number[-4:]


def handler(event, context):
    order_id = (event.get("pathParameters") or {}).get("order_id", "")
    if not common.ORDER_ID_RE.match(order_id):
        return common.json_response(400, {"error": "Invalid order id"})

    order = states.get_order(order_id)
    if not order:
        return common.json_response(404, {"error": "Order not found"})

    public = {key: order[key] for key in ORDER_FIELDS if key in order}
    public["meter_number"] = _mask(order.get("meter_number"))

    timeline = [
        {
            "timestamp": item.get("timestamp"),
            "from_status": item.get("from_status"),
            "to_status": item.get("to_status"),
            "actor": item.get("actor"),
            "detail": item.get("detail"),
        }
        for item in states.get_events(order_id)
    ]
    return common.json_response(200, {"order": public, "events": timeline})
