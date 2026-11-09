"""
End-to-end tests: the REAL gateway, supplier, callback receiver, delivery
worker and reconciler code, wired together against a small in-memory fake of
AWS (tables, queues, secret). They prove the headline claims:

  - a duplicate callback never creates a second token
  - a lost callback is recovered by the reconciler
  - a supplier that issues a token but never replies is NOT refunded
  - a supplier that never issues a token IS refunded, exactly once

Free, instant, and no AWS account needed.
Run from the project root:   py -3.12 -m pytest tests/
"""

import importlib.util
import itertools
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "layer" / "python"))

from ttt import common, states  # noqa: E402


def load(name, folder):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / folder / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock_gateway = load("t_mock_gateway", "mock_gateway")
mock_utility = load("t_mock_utility", "mock_utility")
callback_receiver = load("t_callback_receiver", "callback_receiver")
delivery_worker = load("t_delivery_worker", "delivery_worker")
reconciler = load("t_reconciler", "reconciler")

API = "https://api.test"
SECRET = "test-secret"


# Real DynamoDB refuses these words as bare attribute names in expressions
# (they must be aliased as #name). Neither moto nor a simple fake notices, which
# let a real bug through once, so the fake enforces the rule too. This is a
# subset of DynamoDB's full list of reserved words.
RESERVED_WORDS = {
    "COUNT", "DATA", "HASH", "KEY", "NAME", "RANGE", "REFERENCE", "REFERENCES",
    "ROLE", "SIZE", "SOURCE", "STATE", "STATUS", "TIMESTAMP", "TYPE", "USER", "VALUE",
}
EXPRESSION_KEYWORDS = {"SET", "AND", "OR", "NOT", "ATTRIBUTE_NOT_EXISTS", "ATTRIBUTE_EXISTS"}


class FakeTable:
    """Just enough of a DynamoDB Table for the handlers in this project."""

    def __init__(self, key_name):
        self.key_name = key_name
        self.items = {}

    @staticmethod
    def _check_reserved(expression):
        import re

        bare = re.sub(r"[#:]\w+", "", expression)  # drop aliases and value placeholders
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", bare):
            if word.upper() in RESERVED_WORDS and word.upper() not in EXPRESSION_KEYWORDS:
                raise ClientError(
                    {"Error": {"Code": "ValidationException",
                               "Message": f"Attribute name is a reserved keyword; reserved keyword: {word}"}},
                    "DynamoDB",
                )

    @staticmethod
    def _conditional_failure(operation):
        return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, operation)

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None):
        if ConditionExpression:
            self._check_reserved(ConditionExpression)
        key = Item[self.key_name]
        if ConditionExpression and ConditionExpression.startswith("attribute_not_exists"):
            if key in self.items:
                raise self._conditional_failure("PutItem")
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key[self.key_name])
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key[self.key_name], None)

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues):
        # Supports the one update the mock gateway makes (a guarded refund).
        self._check_reserved(UpdateExpression)
        self._check_reserved(ConditionExpression)
        assert ConditionExpression == "#s = :paid", ConditionExpression
        item = self.items.get(Key[self.key_name])
        if not item or item["status"] != ExpressionAttributeValues[":paid"]:
            raise self._conditional_failure("UpdateItem")
        item["status"] = ExpressionAttributeValues[":refunded"]
        item["refunded_at"] = ExpressionAttributeValues[":now"]


class FakeSQS:
    def __init__(self):
        self.queues = {"outbox": [], "delivery": []}

    def send_message(self, QueueUrl, MessageBody, DelaySeconds=0):
        self.queues[QueueUrl].append(MessageBody)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.orders = {}
        self.events = []
        self.tables = {
            "GATEWAY_TABLE": FakeTable("order_id"),
            "SUPPLIER_TABLE": FakeTable("reference"),
            "CHAOS_TABLE": FakeTable("name"),
            "PROCESSED_TABLE": FakeTable("gateway_reference"),
        }
        self.sqs = FakeSQS()

        env = {
            "GATEWAY_URL": API,
            "UTILITY_URL": API,
            "OUTBOX_QUEUE_URL": "outbox",
            "DELIVERY_QUEUE_URL": "delivery",
            "SLOW_SECONDS": "0",
        }
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(common, "table", lambda name: self.tables[name]),
            mock.patch.object(common, "client", lambda name: self.sqs),
            mock.patch.object(common, "get_secret", lambda: SECRET),
            mock.patch.object(common, "http_request", self._http),
            mock.patch.object(states, "transition", self._transition),
            mock.patch.object(states, "get_order", lambda oid: dict(self.orders[oid]) if oid in self.orders else None),
            mock.patch.object(reconciler, "_find", self._find),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # ---------- fake world ----------

    def _transition(self, order_id, from_status, to_status, actor, detail="", updates=None):
        if to_status not in states.VALID_TRANSITIONS.get(from_status, set()):
            raise states.InvalidTransition(f"{from_status} -> {to_status}")
        order = self.orders[order_id]
        if order["status"] != from_status:
            raise states.StaleState(order_id)
        order.update(updates or {})
        order["status"] = to_status
        order["updated_at"] = int(time.time())
        self.events.append((order_id, from_status, to_status, actor))

    def _find(self, status, min_age, now, max_age=None):
        newest = now - min_age
        oldest = now - max_age if max_age else 0
        return [
            dict(o) for o in self.orders.values()
            if o["status"] == status and oldest <= o["updated_at"] <= newest
        ]

    def _http(self, method, url, body=None, headers=None, timeout=3.0):
        """Stand-in for API Gateway: route the request to the real handler."""
        path = url[len(API):]
        params = None
        if path.startswith("/gateway/status/"):
            route, params, fn = "GET /gateway/status/{order_id}", {"order_id": path.rsplit("/", 1)[1]}, mock_gateway
        elif path.startswith("/utility/status/"):
            route, params, fn = "GET /utility/status/{reference}", {"reference": path.rsplit("/", 1)[1]}, mock_utility
        else:
            route = f"{method} {path}"
            fn = {
                "POST /gateway/pay": mock_gateway,
                "POST /gateway/refund": mock_gateway,
                "POST /utility/token": mock_utility,
                "POST /callbacks/payment": callback_receiver,
            }[route]
        event = {"routeKey": route, "body": body or "", "headers": headers or {}, "pathParameters": params}
        response = fn.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ---------- helpers ----------

    def new_order(self, order_id="order-0001", amount=1000):
        self.orders[order_id] = {
            "order_id": order_id, "status": states.PENDING, "amount_cents": amount,
            "meter_number": "01234567890", "updated_at": int(time.time()),
        }

    def pay(self, order_id="order-0001", amount=1000, force="none"):
        status, data = self._http(
            "POST", f"{API}/gateway/pay",
            json.dumps({"order_id": order_id, "amount_cents": amount, "force": force}),
        )
        self.assertEqual(status, 200, data)
        return data

    def send_callback(self, body, signature=None):
        headers = {"x-gateway-signature": signature or common.sign(body, SECRET)}
        return self._http("POST", f"{API}/callbacks/payment", body, headers)

    def deliver_outbox(self):
        """What the callback-sender Lambda does: sign each message and POST it."""
        results = []
        messages, self.sqs.queues["outbox"] = self.sqs.queues["outbox"], []
        for body in messages:
            status, data = self.send_callback(body)
            results.append(data.get("result"))
        return results

    def run_worker(self):
        """What the delivery-worker trigger does. Returns the number of failures."""
        failures = 0
        messages, self.sqs.queues["delivery"] = self.sqs.queues["delivery"], []
        for body in messages:
            out = delivery_worker.handler({"Records": [{"messageId": "m", "body": body}]}, None)
            failures += len(out["batchItemFailures"])
        return failures

    def age(self, order_id, seconds):
        self.orders[order_id]["updated_at"] -= seconds

    def set_chaos(self, **values):
        self.tables["CHAOS_TABLE"].items["chaos"] = {"name": "chaos", **values}

    def tokens_issued(self):
        return len(self.tables["SUPPLIER_TABLE"].items)

    # ---------- the headline claims ----------

    def test_happy_path_ends_in_one_token(self):
        self.new_order()
        self.pay()
        self.assertEqual(self.deliver_outbox(), ["accepted"])
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.tokens_issued(), 1)

    def test_duplicate_callback_never_creates_a_second_token(self):
        self.new_order()
        self.assertEqual(self.pay(force="duplicate")["callback"], "duplicated")
        self.assertEqual(len(self.sqs.queues["outbox"]), 2)  # the gateway really sent it twice

        self.assertEqual(self.deliver_outbox(), ["accepted", "duplicate"])
        self.assertEqual(len(self.sqs.queues["delivery"]), 1)  # only one delivery queued

        self.run_worker()
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.tokens_issued(), 1)

    def test_the_same_delivery_message_processed_twice_is_harmless(self):
        self.new_order()
        self.pay()
        self.deliver_outbox()
        message = self.sqs.queues["delivery"][0]
        self.run_worker()
        self.sqs.queues["delivery"].append(message)  # SQS can deliver a message twice
        self.run_worker()
        self.assertEqual(self.tokens_issued(), 1)
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)

    def test_lost_callback_is_recovered_by_the_reconciler(self):
        self.new_order()
        self.assertEqual(self.pay(force="lost")["callback"], "lost")
        self.assertEqual(self.deliver_outbox(), [])  # nothing ever arrives
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)

        self.age("order-0001", 600)
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)

        self.run_worker()
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.tokens_issued(), 1)

    def test_a_late_callback_after_the_reconciler_does_no_harm(self):
        self.new_order()
        self.pay(force="delayed")
        late_callbacks = list(self.sqs.queues["outbox"])
        self.sqs.queues["outbox"].clear()  # not delivered yet

        self.age("order-0001", 600)
        reconciler.handler({}, None)
        self.run_worker()
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)

        self.sqs.queues["outbox"].extend(late_callbacks)  # now it finally arrives
        self.deliver_outbox()
        self.run_worker()
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.tokens_issued(), 1)

    def test_supplier_issues_token_but_reply_is_lost_no_refund_no_double_token(self):
        self.new_order()
        self.pay()
        self.deliver_outbox()

        self.set_chaos(provider_timeout_rate=1.0)
        with mock.patch("random.random", side_effect=itertools.cycle([0.0, 0.0])):  # issued, reply lost
            self.assertEqual(self.run_worker(), 1)  # the worker saw a failure
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.tokens_issued(), 1)  # ...but the supplier DID issue one

        self.age("order-0001", 600)
        counts = reconciler.handler({}, None)
        self.assertEqual(counts["token_recovered"], 1)
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.tokens_issued(), 1)
        self.assertEqual(self.tables["GATEWAY_TABLE"].items["order-0001"]["status"], "PAID")  # not refunded

    def test_a_retry_after_a_timeout_returns_the_same_token(self):
        self.new_order()
        self.pay()
        self.deliver_outbox()
        self.set_chaos(provider_timeout_rate=1.0)
        with mock.patch("random.random", side_effect=itertools.cycle([0.0, 0.0])):
            self.run_worker()
        issued = next(iter(self.tables["SUPPLIER_TABLE"].items.values()))["token"]

        self.set_chaos(provider_timeout_rate=0.0)
        self.sqs.queues["delivery"].append(json.dumps({"order_id": "order-0001"}))
        self.run_worker()
        self.assertEqual(self.orders["order-0001"]["token"], issued)
        self.assertEqual(self.tokens_issued(), 1)

    def test_supplier_never_issues_so_the_customer_is_refunded_exactly_once(self):
        self.new_order()
        self.pay()
        self.deliver_outbox()

        self.set_chaos(provider_timeout_rate=1.0)
        with mock.patch("random.random", side_effect=itertools.cycle([0.0, 0.9])):  # not issued
            self.assertEqual(self.run_worker(), 1)
        self.assertEqual(self.tokens_issued(), 0)

        self.age("order-0001", 1500)  # past the give-up time
        counts = reconciler.handler({}, None)
        self.assertEqual(counts["refunded"], 1)
        self.assertEqual(self.orders["order-0001"]["status"], states.REFUNDED)
        self.assertEqual(self.tables["GATEWAY_TABLE"].items["order-0001"]["status"], "REFUNDED")
        self.assertEqual(self.tokens_issued(), 0)

        self.age("order-0001", 1000)
        reconciler.handler({}, None)  # running again must not refund again
        self.assertEqual(sum(1 for e in self.events if e[2] == states.REFUNDED), 1)

    # ---------- callback safety ----------

    def test_a_callback_with_a_bad_signature_is_rejected(self):
        self.new_order()
        self.pay()
        body = self.sqs.queues["outbox"].pop()
        status, data = self.send_callback(body, signature="not-the-real-signature")
        self.assertEqual(status, 401)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)

    def test_a_callback_with_the_wrong_amount_is_rejected(self):
        self.new_order(amount=1000)
        body = json.dumps({"gateway_reference": "gw-x", "order_id": "order-0001",
                           "amount_cents": 1, "status": "Paid"})
        status, data = self.send_callback(body)
        self.assertEqual(status, 422)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)

    def test_a_cancelled_payment_marks_the_order_failed_without_crashing(self):
        self.new_order()
        body = json.dumps({"gateway_reference": "gw-c", "order_id": "order-0001",
                           "amount_cents": 1000, "status": "Cancelled"})
        status, data = self.send_callback(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["result"], "cancelled")
        self.assertEqual(self.orders["order-0001"]["status"], states.FAILED)


if __name__ == "__main__":
    unittest.main()
