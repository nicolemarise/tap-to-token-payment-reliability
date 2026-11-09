"""
Tests for the reconciler's decisions.

These fake the outside world (the orders table, the gateway, the supplier and
the delivery queue) so they run anywhere, instantly, at no cost. They check
the part that matters most: what the reconciler DECIDES for each situation,
and above all that it never refunds on an unknown answer.

Run from the project root:   py -3.12 -m pytest tests/
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "layer" / "python"))

from ttt import common, states  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "reconciler_handler", ROOT / "src" / "reconciler" / "handler.py"
)
reconciler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconciler)

API = "https://api.test"


class ReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.orders = {}  # order_id -> order dict
        self.events = []  # (order_id, from, to, actor)
        self.queued = []  # order_ids put on the delivery queue
        self.calls = []  # (method, path) of every outbound HTTP call
        self.responses = {}  # (method, path) -> (status, body) or an Exception

        env = {"GATEWAY_URL": API, "UTILITY_URL": API}
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(reconciler, "_find", self._find),
            mock.patch.object(states, "transition", self._transition),
            mock.patch.object(common, "http_request", self._http),
            mock.patch.object(common, "enqueue_delivery", self.queued.append),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # ---------- fakes ----------

    def _find(self, status, min_age, now, max_age=None):
        newest = now - min_age
        oldest = now - max_age if max_age else 0
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] == status and oldest <= o["updated_at"] <= newest
        ]

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

    def _http(self, method, url, body=None, headers=None, timeout=3.0):
        path = url[len(API):]
        self.calls.append((method, path))
        response = self.responses.get((method, path))
        if response is None:
            raise AssertionError(f"unexpected call: {method} {path}")
        if isinstance(response, Exception):
            raise response
        return response

    # ---------- helpers ----------

    def add_order(self, order_id, status, age_seconds, amount=1000):
        self.orders[order_id] = {
            "order_id": order_id,
            "status": status,
            "updated_at": int(time.time()) - age_seconds,
            "amount_cents": amount,
            "meter_number": "01234567890",
        }

    def gateway_says(self, order_id, status, body):
        self.responses[("GET", f"/gateway/status/{order_id}")] = (status, body)

    def supplier_says(self, order_id, status, body):
        self.responses[("GET", f"/utility/status/{order_id}")] = (status, body)

    def refund_says(self, status, body):
        self.responses[("POST", "/gateway/refund")] = (status, body)

    def refund_calls(self):
        return [c for c in self.calls if c == ("POST", "/gateway/refund")]

    # ---------- PENDING: the callback was lost ----------

    def test_lost_callback_is_recovered_and_delivery_queued(self):
        self.add_order("order-0001", states.PENDING, 600)
        self.gateway_says(
            "order-0001", 200, {"status": "PAID", "gateway_reference": "gw-1", "amount_cents": 1000}
        )
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.orders["order-0001"]["gateway_reference"], "gw-1")
        self.assertEqual(self.queued, ["order-0001"])
        self.assertEqual(counts["pending_recovered"], 1)

    def test_recent_pending_order_is_left_alone(self):
        self.add_order("order-0001", states.PENDING, 10)
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)
        self.assertEqual(self.calls, [])

    def test_gateway_amount_mismatch_is_not_acted_on(self):
        self.add_order("order-0001", states.PENDING, 600, amount=1000)
        self.gateway_says(
            "order-0001", 200, {"status": "PAID", "gateway_reference": "gw-1", "amount_cents": 999}
        )
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)
        self.assertEqual(self.queued, [])
        self.assertEqual(counts["errors"], 1)

    def test_order_never_paid_is_abandoned_after_long_wait(self):
        self.add_order("order-0001", states.PENDING, 2000)
        self.gateway_says("order-0001", 404, {"status": "NOT_FOUND"})
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.FAILED)
        self.assertEqual(counts["pending_abandoned"], 1)
        self.assertEqual(self.refund_calls(), [])  # nothing was paid, nothing to refund

    def test_order_never_paid_is_kept_until_the_wait_is_over(self):
        self.add_order("order-0001", states.PENDING, 600)
        self.gateway_says("order-0001", 404, {"status": "NOT_FOUND"})
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)

    def test_gateway_unreachable_changes_nothing(self):
        self.add_order("order-0001", states.PENDING, 5000)
        self.responses[("GET", "/gateway/status/order-0001")] = common.TransientError("timeout")
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PENDING)

    def test_callback_arriving_during_the_check_wins_cleanly(self):
        self.add_order("order-0001", states.PENDING, 600)
        self.gateway_says(
            "order-0001", 200, {"status": "PAID", "gateway_reference": "gw-1", "amount_cents": 1000}
        )

        def callback_got_there_first(*args, **kwargs):
            self.orders["order-0001"]["status"] = states.PAID
            raise states.StaleState("order-0001")

        with mock.patch.object(states, "transition", callback_got_there_first):
            counts = reconciler.handler({}, None)
        self.assertEqual(self.queued, [])  # the callback queued it, we do not double up
        self.assertEqual(counts["pending_recovered"], 0)

    # ---------- PAID: did the supplier issue a token? ----------

    def test_token_issued_but_reply_lost_is_completed_without_refund(self):
        self.add_order("order-0001", states.PAID, 600)
        self.supplier_says("order-0001", 200, {"issued": True, "token": "1111-2222-3333-4444-5555"})
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(self.orders["order-0001"]["token"], "1111-2222-3333-4444-5555")
        self.assertEqual(self.refund_calls(), [])
        self.assertEqual(counts["token_recovered"], 1)

    def test_no_token_yet_means_try_delivery_again_not_refund(self):
        self.add_order("order-0001", states.PAID, 600)  # younger than give-up time
        self.supplier_says("order-0001", 200, {"issued": False})
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.queued, ["order-0001"])
        self.assertEqual(self.refund_calls(), [])
        self.assertEqual(counts["redelivery_queued"], 1)

    def test_no_token_after_giving_up_fails_and_refunds(self):
        self.add_order("order-0001", states.PAID, 1500)
        self.supplier_says("order-0001", 200, {"issued": False})
        self.refund_says(200, {"refunded": True, "already_refunded": False})
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.REFUNDED)
        self.assertEqual(len(self.refund_calls()), 1)
        self.assertEqual(
            [(e[1], e[2]) for e in self.events],
            [(states.PAID, states.FAILED), (states.FAILED, states.REFUNDED)],
        )
        self.assertEqual(counts["refunded"], 1)

    def test_supplier_unreachable_never_refunds(self):
        self.add_order("order-0001", states.PAID, 99999)
        self.responses[("GET", "/utility/status/order-0001")] = common.TransientError("timeout")
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.refund_calls(), [])

    def test_supplier_error_status_never_refunds(self):
        self.add_order("order-0001", states.PAID, 99999)
        self.supplier_says("order-0001", 500, {"error": "boom"})
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.refund_calls(), [])

    def test_unexpected_supplier_answer_never_refunds(self):
        self.add_order("order-0001", states.PAID, 99999)
        self.supplier_says("order-0001", 200, {"something": "else"})
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.PAID)
        self.assertEqual(self.refund_calls(), [])

    # ---------- FAILED: make sure the refund really happened ----------

    def test_refund_that_failed_is_retried_on_the_next_run(self):
        self.add_order("order-0001", states.PAID, 1500)
        self.supplier_says("order-0001", 200, {"issued": False})
        self.responses[("POST", "/gateway/refund")] = common.TransientError("timeout")
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.FAILED)

        self.orders["order-0001"]["updated_at"] -= 1000  # time passes
        self.refund_says(200, {"refunded": True, "already_refunded": False})
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.REFUNDED)

    def test_failed_order_with_no_payment_stays_failed(self):
        self.add_order("order-0001", states.FAILED, 600)
        self.refund_says(404, {"error": "No payment found for this order"})
        reconciler.handler({}, None)
        self.assertEqual(self.orders["order-0001"]["status"], states.FAILED)

    # ---------- Safety ----------

    def test_running_twice_does_not_queue_delivery_twice(self):
        self.add_order("order-0001", states.PENDING, 600)
        self.gateway_says(
            "order-0001", 200, {"status": "PAID", "gateway_reference": "gw-1", "amount_cents": 1000}
        )
        reconciler.handler({}, None)
        reconciler.handler({}, None)  # the order is now freshly PAID, so it is not stale
        self.assertEqual(self.queued, ["order-0001"])

    def test_one_bad_order_does_not_stop_the_others(self):
        self.add_order("order-bad1", states.PAID, 600)
        self.add_order("order-good", states.PAID, 600)
        self.responses[("GET", "/utility/status/order-bad1")] = RuntimeError("boom")
        self.supplier_says("order-good", 200, {"issued": True, "token": "9999-9999-9999-9999-9999"})
        counts = reconciler.handler({}, None)
        self.assertEqual(self.orders["order-good"]["status"], states.TOKEN_ISSUED)
        self.assertEqual(counts["errors"], 1)


class CallbackReceiverFixTests(unittest.TestCase):
    def test_a_cancelled_payment_uses_a_state_that_exists(self):
        """The receiver once referenced states.CANCELLED, which does not exist."""
        source = (ROOT / "src" / "callback_receiver" / "handler.py").read_text()
        self.assertNotIn("states.CANCELLED", source)
        self.assertIn("PENDING", states.VALID_TRANSITIONS)
        self.assertIn(states.FAILED, states.VALID_TRANSITIONS[states.PENDING])


if __name__ == "__main__":
    unittest.main()
