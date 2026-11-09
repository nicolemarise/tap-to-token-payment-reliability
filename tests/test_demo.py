"""
Tests for the demo page and the read-only "get order" endpoint.

Run from the project root:   py -3.12 -m pytest tests/
"""

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "layer" / "python"))

from ttt import states  # noqa: E402


def load(name, folder):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / folder / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


get_order = load("t_get_order", "get_order")
demo_page = load("t_demo_page", "demo_page")

ORDER = {
    "order_id": "order-0001",
    "status": "TOKEN_ISSUED",
    "meter_number": "01234567890",
    "amount_cents": 1000,
    "currency": "USD",
    "token": "1111-2222-3333-4444-5555",
    "created_at": 1794180111,
    "updated_at": 1794180115,
    "gateway_reference": "gw-secret-internal",
    "idempotency_hash": "internal",
}
EVENTS = [
    {"order_id": "order-0001", "event_key": "x", "timestamp": 1794180111, "from_status": "NONE",
     "to_status": "PENDING", "actor": "api", "detail": "Order created"},
    {"order_id": "order-0001", "event_key": "y", "timestamp": 1794180115, "from_status": "PENDING",
     "to_status": "PAID", "actor": "gateway-callback", "detail": "Payment confirmed"},
]


def call(order_id):
    return get_order.handler({"pathParameters": {"order_id": order_id}}, None)


class GetOrderTests(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(states, "get_order", lambda oid: dict(ORDER) if oid == "order-0001" else None),
            mock.patch.object(states, "get_events", lambda oid: list(EVENTS)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_returns_the_order_and_its_audit_log_in_order(self):
        response = call("order-0001")
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["order"]["status"], "TOKEN_ISSUED")
        self.assertEqual([e["to_status"] for e in body["events"]], ["PENDING", "PAID"])

    def test_the_meter_number_is_masked(self):
        body = json.loads(call("order-0001")["body"])
        self.assertEqual(body["order"]["meter_number"], "*******7890")

    def test_internal_fields_are_not_exposed(self):
        text = call("order-0001")["body"]
        self.assertNotIn("gw-secret-internal", text)
        self.assertNotIn("idempotency_hash", text)
        self.assertNotIn("event_key", text)

    def test_unknown_order_is_a_404(self):
        self.assertEqual(call("order-9999")["statusCode"], 404)

    def test_an_invalid_id_is_a_400_and_never_reaches_the_database(self):
        with mock.patch.object(states, "get_order", side_effect=AssertionError("should not be called")):
            self.assertEqual(call("../etc/passwd")["statusCode"], 400)
            self.assertEqual(call("x")["statusCode"], 400)


class DemoPageTests(unittest.TestCase):
    def setUp(self):
        self.response = demo_page.handler({}, None)
        self.html = self.response["body"]

    def test_serves_the_page_as_html(self):
        self.assertEqual(self.response["statusCode"], 200)
        self.assertIn("text/html", self.response["headers"]["Content-Type"])
        self.assertIn("Tap to Token", self.html)

    def test_sends_security_headers(self):
        headers = self.response["headers"]
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        policy = headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", policy)  # may only call its own API
        self.assertIn("frame-ancestors 'none'", policy)

    def test_never_writes_server_data_as_html(self):
        # Everything the API returns is shown with textContent, so it cannot inject markup.
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(risky, self.html)

    def test_only_calls_its_own_api(self):
        paths = re.findall(r'api\(\s*"[A-Z]+",\s*"([^"]*)"', self.html)
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/"), path)

    def test_offers_every_failure_scenario(self):
        for scenario in ("ok", "double", "dup", "lost", "late", "timeout"):
            self.assertIn(f'id: "{scenario}"', self.html)


if __name__ == "__main__":
    unittest.main()
