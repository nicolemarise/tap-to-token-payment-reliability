"""
Order state machine + audit log for Tap to Token.

Every Lambda imports this module (via a Lambda layer), so the rules for
moving an order between states live in exactly one place.

Valid flow:
    PENDING -> PAID -> TOKEN_ISSUED          (success)
    PENDING -> PAID -> FAILED -> REFUNDED    (delivery failed, customer refunded)
    PENDING -> FAILED                        (payment never completed)

Every change is written as ONE DynamoDB transaction containing:
    1. the order update (guarded by a condition on the current status)
    2. an audit event describing the change
so the audit log can never disagree with the order itself.
"""

import os
import time
import uuid

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

PENDING = "PENDING"
PAID = "PAID"
TOKEN_ISSUED = "TOKEN_ISSUED"
FAILED = "FAILED"
REFUNDED = "REFUNDED"

VALID_TRANSITIONS = {
    PENDING: {PAID, FAILED},
    PAID: {TOKEN_ISSUED, FAILED},
    FAILED: {REFUNDED},
    TOKEN_ISSUED: set(),  # terminal
    REFUNDED: set(),  # terminal
}


class OrderAlreadyExists(Exception):
    """An order with this order_id (idempotency key) already exists."""


class InvalidTransition(Exception):
    """The requested status change is not allowed by the state machine."""


class StaleState(Exception):
    """The order was not in the status we expected (someone else moved it)."""


_serializer = TypeSerializer()
_deserializer = TypeDeserializer()
_client = None


def _ddb():
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _orders_table():
    return os.environ["ORDERS_TABLE"]


def _events_table():
    return os.environ["EVENTS_TABLE"]


def _to_ddb(item):
    return {k: _serializer.serialize(v) for k, v in item.items()}


def _from_ddb(item):
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


def _event_item(order_id, from_status, to_status, actor, detail):
    now_ms = int(time.time() * 1000)
    return {
        "order_id": order_id,
        "event_key": f"{now_ms:013d}#{uuid.uuid4().hex[:8]}",
        "timestamp": now_ms // 1000,
        "from_status": from_status or "NONE",
        "to_status": to_status,
        "actor": actor,
        "detail": detail,
    }


def _is_conditional_failure(error):
    """True if a cancelled transaction failed because a condition was not met."""
    reasons = error.response.get("CancellationReasons", [])
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


def create_order(order, actor="api"):
    """
    Insert a brand-new order in status PENDING, plus its first audit event.
    Raises OrderAlreadyExists if order_id is already taken.
    """
    event = _event_item(order["order_id"], None, PENDING, actor, "Order created")
    try:
        _ddb().transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": _orders_table(),
                        "Item": _to_ddb(order),
                        "ConditionExpression": "attribute_not_exists(order_id)",
                    }
                },
                {"Put": {"TableName": _events_table(), "Item": _to_ddb(event)}},
            ]
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "TransactionCanceledException" and (
            _is_conditional_failure(error)
        ):
            raise OrderAlreadyExists(order["order_id"]) from error
        raise


def transition(order_id, from_status, to_status, actor, detail="", updates=None):
    """
    Move an order from from_status to to_status, atomically with an audit event.

    - Raises InvalidTransition if the state machine does not allow this move.
    - Raises StaleState if the order is not currently in from_status
      (for example, a duplicate callback or a racing worker got there first).
    - `updates` is an optional dict of extra attributes to set on the order
      (e.g. {"token": "1234-5678"}).
    """
    if to_status not in VALID_TRANSITIONS.get(from_status, set()):
        raise InvalidTransition(f"{from_status} -> {to_status} is not allowed")

    updates = dict(updates or {})
    updates["status"] = to_status
    updates["updated_at"] = int(time.time())

    names = {}
    values = {":expected": _serializer.serialize(from_status)}
    set_clauses = []
    for i, (key, value) in enumerate(updates.items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = _serializer.serialize(value)
        set_clauses.append(f"#k{i} = :v{i}")
    names["#cur"] = "status"

    event = _event_item(order_id, from_status, to_status, actor, detail)
    try:
        _ddb().transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": _orders_table(),
                        "Key": {"order_id": {"S": order_id}},
                        "UpdateExpression": "SET " + ", ".join(set_clauses),
                        "ConditionExpression": "#cur = :expected",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                },
                {"Put": {"TableName": _events_table(), "Item": _to_ddb(event)}},
            ]
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "TransactionCanceledException" and (
            _is_conditional_failure(error)
        ):
            raise StaleState(f"{order_id} is not in {from_status}") from error
        raise


def get_order(order_id):
    """Return the order as a plain dict, or None if it does not exist."""
    response = _ddb().get_item(
        TableName=_orders_table(),
        Key={"order_id": {"S": order_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return _from_ddb(item) if item else None


def get_events(order_id):
    """Return the audit log for an order, oldest first."""
    response = _ddb().query(
        TableName=_events_table(),
        KeyConditionExpression="order_id = :o",
        ExpressionAttributeValues={":o": {"S": order_id}},
        ScanIndexForward=True,
    )
    return [_from_ddb(item) for item in response.get("Items", [])]
