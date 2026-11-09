"""
Local tests for the state machine and create-order handler.
Uses moto to fake DynamoDB, so no AWS account or cost is involved.

Run:  pip install boto3 moto pytest  &&  pytest tests/
"""

import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src", "layer", "python"))
sys.path.insert(0, os.path.join(ROOT, "src", "create_order"))

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ["ORDERS_TABLE"] = "orders"
os.environ["EVENTS_TABLE"] = "events"


@pytest.fixture
def tables():
    with mock_aws():
        from ttt import states

        states._client = None  # make sure the client is created inside the mock
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="events",
            KeySchema=[
                {"AttributeName": "order_id", "KeyType": "HASH"},
                {"AttributeName": "event_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "order_id", "AttributeType": "S"},
                {"AttributeName": "event_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield states


def _event(key="order-12345678", meter="01234567890", amount=1000):
    return {
        "headers": {"Idempotency-Key": key} if key else {},
        "body": json.dumps({"meter_number": meter, "amount_cents": amount}),
    }


def test_first_request_creates_order(tables):
    import handler

    res = handler.handler(_event(), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 201
    assert body["order"]["status"] == "PENDING"
    assert body["idempotent_replay"] is False


def test_double_tap_returns_same_order_and_creates_nothing_new(tables):
    import handler

    handler.handler(_event(), None)
    res = handler.handler(_event(), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 200
    assert body["idempotent_replay"] is True
    # Exactly one audit event, even though two requests arrived
    assert len(tables.get_events("order-12345678")) == 1


def test_same_key_different_request_is_rejected(tables):
    import handler

    handler.handler(_event(amount=1000), None)
    res = handler.handler(_event(amount=2000), None)
    assert res["statusCode"] == 409


def test_missing_idempotency_key_is_rejected(tables):
    import handler

    assert handler.handler(_event(key=None), None)["statusCode"] == 400


def test_bad_input_is_rejected(tables):
    import handler

    assert handler.handler(_event(meter="abc"), None)["statusCode"] == 400
    assert handler.handler(_event(amount=5), None)["statusCode"] == 400


def test_happy_path_and_audit_log(tables):
    import handler

    handler.handler(_event(), None)
    tables.transition("order-12345678", "PENDING", "PAID", "callback")
    tables.transition(
        "order-12345678", "PAID", "TOKEN_ISSUED", "worker", updates={"token": "1111"}
    )
    order = tables.get_order("order-12345678")
    assert order["status"] == "TOKEN_ISSUED"
    assert order["token"] == "1111"
    trail = [(e["from_status"], e["to_status"]) for e in tables.get_events("order-12345678")]
    assert trail == [("NONE", "PENDING"), ("PENDING", "PAID"), ("PAID", "TOKEN_ISSUED")]


def test_invalid_transition_is_blocked(tables):
    import handler

    handler.handler(_event(), None)
    with pytest.raises(tables.InvalidTransition):
        tables.transition("order-12345678", "PENDING", "TOKEN_ISSUED", "bug")


def test_duplicate_callback_cannot_move_order_twice(tables):
    import handler

    handler.handler(_event(), None)
    tables.transition("order-12345678", "PENDING", "PAID", "callback")
    with pytest.raises(tables.StaleState):
        tables.transition("order-12345678", "PENDING", "PAID", "callback-duplicate")
    # The failed attempt must not have written an audit event
    assert len(tables.get_events("order-12345678")) == 2
