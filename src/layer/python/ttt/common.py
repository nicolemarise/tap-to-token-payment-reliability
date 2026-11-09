"""Shared helpers for the Tap to Token Lambdas (HTTP, signing, chaos, queues)."""

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal

import boto3

ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_clients = {}
_secret_cache = {}


class TransientError(Exception):
    """A network problem (timeout, connection refused). Safe to retry."""


def reset_caches():
    """Used by tests so clients are created inside the fake AWS."""
    _clients.clear()
    _secret_cache.clear()


def client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def table(env_name):
    """A DynamoDB Table resource for the table named in an environment variable."""
    if "ddb-resource" not in _clients:
        _clients["ddb-resource"] = boto3.resource("dynamodb")
    return _clients["ddb-resource"].Table(os.environ[env_name])


# ---------- HTTP responses and bodies ----------


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Cannot serialise {type(value)}")


def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def raw_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def parse_body(event):
    """Parse a JSON object body, or raise ValueError with a friendly message."""
    try:
        data = json.loads(raw_body(event) or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Body must be valid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("Body must be a JSON object")
    return data


# ---------- Outbound HTTP ----------


def http_request(method, url, body=None, headers=None, timeout=3.0):
    """
    Make an HTTP call. Returns (status_code, parsed_json_or_text).
    HTTP error statuses are RETURNED, not raised. Network failures and
    timeouts raise TransientError.
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )

    def parse(raw):
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, parse(response.read())
    except urllib.error.HTTPError as error:
        return error.code, parse(error.read())
    except OSError as error:  # URLError, socket timeouts, connection resets
        raise TransientError(str(error)) from error


# ---------- Signing (gateway -> callback) ----------


def get_secret():
    secret_id = os.environ["SECRET_ID"]
    if secret_id not in _secret_cache:
        response = client("secretsmanager").get_secret_value(SecretId=secret_id)
        _secret_cache[secret_id] = response["SecretString"]
    return _secret_cache[secret_id]


def sign(body, secret):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify(body, signature, secret):
    return hmac.compare_digest(sign(body, secret), signature or "")


# ---------- Chaos switch ----------

CHAOS_DEFAULTS = {
    "duplicate_callback_rate": 0.0,
    "delayed_callback_rate": 0.0,
    "lost_callback_rate": 0.0,
    "provider_timeout_rate": 0.0,
    "delayed_seconds": 90.0,
}


def get_chaos():
    chaos = dict(CHAOS_DEFAULTS)
    item = table("CHAOS_TABLE").get_item(Key={"name": "chaos"}).get("Item")
    if item:
        for key in CHAOS_DEFAULTS:
            if key in item:
                chaos[key] = float(item[key])
    return chaos


def set_chaos(values):
    item = {"name": "chaos"}
    for key in CHAOS_DEFAULTS:
        item[key] = Decimal(str(values[key]))
    table("CHAOS_TABLE").put_item(Item=item)


# ---------- Delivery queue ----------


def enqueue_delivery(order_id):
    client("sqs").send_message(
        QueueUrl=os.environ["DELIVERY_QUEUE_URL"],
        MessageBody=json.dumps({"order_id": order_id}),
    )
