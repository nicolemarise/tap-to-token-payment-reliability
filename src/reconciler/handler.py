"""
Reconciler: runs on a schedule and resolves orders that are stuck.

Callbacks can be lost and suppliers can go quiet, so some orders never reach
a final state by themselves. This job finds them and asks the source of truth
(the gateway or the supplier) what really happened before it decides anything.

What it does, per status:

  PENDING for too long  -> ask the gateway "did this order get paid?"
        PAID      : the callback was lost. Move to PAID and queue delivery.
        NOT_FOUND : no payment exists. After a longer wait, mark FAILED.

  PAID for too long     -> ask the supplier "did you issue a token?"
        issued    : the supplier's reply was lost. Record the token.
        not issued: queue delivery again; if we have waited long enough,
                    mark FAILED and refund.

  FAILED                -> make sure the refund went through (retry it).

The one rule that matters most: NEVER refund on an unknown answer. If the
supplier cannot be reached, or answers something unexpected, we do nothing and
try again on the next run. We only refund when the supplier has definitely
said "no token was issued for this reference".

Every change goes through states.transition, which is conditional on the
current status, so running twice (or racing the normal flow) is harmless.

Known limit, stated honestly: if a delivery is still in flight at the exact
moment we give up and refund, a token could be issued after the refund. The
give-up time (20 minutes by default) is far longer than the delivery retry
window (about 8 minutes), which makes this very unlikely, but a real system
would also need a supplier "void token" call.
"""

import json
import os
import time

from boto3.dynamodb.conditions import Key

from ttt import common, states

INDEX = "status-updated_at-index"
MAX_PER_STATUS = 20  # orders handled per status per run, keeps a run short
STOP_WHEN_MS_LEFT = 15000  # stop starting new work when time is nearly up


def _seconds(name, default):
    return int(os.environ.get(name, default))


def _gateway(path):
    return f"{os.environ['GATEWAY_URL']}{path}"


def _utility(path):
    return f"{os.environ['UTILITY_URL']}{path}"


def _log(message):
    print(f"reconciler: {message}")


def _time_left(context):
    if context is None:
        return True
    return context.get_remaining_time_in_millis() > STOP_WHEN_MS_LEFT


def _find(status, min_age, now, max_age=None):
    """Orders in `status` whose last change was at least min_age seconds ago."""
    newest = now - min_age
    oldest = now - max_age if max_age else 0
    response = common.table("ORDERS_TABLE").query(
        IndexName=INDEX,
        KeyConditionExpression=Key("status").eq(status)
        & Key("updated_at").between(oldest, newest),
        Limit=MAX_PER_STATUS,
    )
    return response.get("Items", [])


# ---------- PENDING: was a payment actually taken? ----------


def _recover_pending(order, now, counts):
    order_id = order["order_id"]
    try:
        status, data = common.http_request(
            "GET", _gateway(f"/gateway/status/{order_id}"), timeout=3
        )
    except common.TransientError as error:
        _log(f"{order_id}: gateway unreachable, will retry ({error})")
        counts["skipped"] += 1
        return

    if status == 200 and isinstance(data, dict) and data.get("status") == "PAID":
        reference = str(data.get("gateway_reference", ""))
        if int(data.get("amount_cents", -1)) != int(order["amount_cents"]):
            _log(f"{order_id}: gateway amount does not match the order, leaving it")
            counts["errors"] += 1
            return
        try:
            states.transition(
                order_id,
                states.PENDING,
                states.PAID,
                "reconciler",
                f"Gateway confirms payment ({reference}); the callback never arrived",
                {"gateway_reference": reference},
            )
        except states.StaleState:
            counts["skipped"] += 1  # the callback arrived while we were checking
            return
        common.enqueue_delivery(order_id)
        counts["pending_recovered"] += 1
        return

    if status == 404:
        age = now - int(order["updated_at"])
        if age >= _seconds("ABANDON_PENDING_SECONDS", 1800):
            try:
                states.transition(
                    order_id,
                    states.PENDING,
                    states.FAILED,
                    "reconciler",
                    "No payment was ever made at the gateway; order abandoned",
                )
            except states.StaleState:
                counts["skipped"] += 1
                return
            counts["pending_abandoned"] += 1
            return

    counts["skipped"] += 1


# ---------- PAID: did the supplier issue a token? ----------


def _recover_paid(order, now, counts):
    order_id = order["order_id"]
    try:
        status, data = common.http_request(
            "GET", _utility(f"/utility/status/{order_id}"), timeout=3
        )
    except common.TransientError as error:
        _log(f"{order_id}: supplier unreachable, NOT refunding ({error})")
        counts["skipped"] += 1
        return

    if status != 200 or not isinstance(data, dict):
        _log(f"{order_id}: supplier answered {status}, NOT refunding")
        counts["skipped"] += 1
        return

    if data.get("issued") is True and data.get("token"):
        try:
            states.transition(
                order_id,
                states.PAID,
                states.TOKEN_ISSUED,
                "reconciler",
                "Supplier confirms the token was issued; its reply had been lost",
                {"token": data["token"]},
            )
        except states.StaleState:
            counts["skipped"] += 1
            return
        counts["token_recovered"] += 1
        return

    if data.get("issued") is not False:
        _log(f"{order_id}: unexpected supplier answer, NOT refunding")
        counts["skipped"] += 1
        return

    # The supplier definitely has no token for this order.
    age = now - int(order["updated_at"])
    if age < _seconds("GIVE_UP_SECONDS", 1200):
        common.enqueue_delivery(order_id)
        counts["redelivery_queued"] += 1
        return

    try:
        states.transition(
            order_id,
            states.PAID,
            states.FAILED,
            "reconciler",
            "Supplier confirms no token was issued; giving up and refunding",
        )
    except states.StaleState:
        counts["skipped"] += 1
        return
    counts["failed"] += 1
    _refund(order_id, counts)


# ---------- FAILED: make sure the customer got their money back ----------


def _refund(order_id, counts):
    try:
        status, data = common.http_request(
            "POST",
            _gateway("/gateway/refund"),
            json.dumps({"order_id": order_id}),
            {"Content-Type": "application/json"},
            timeout=3,
        )
    except common.TransientError as error:
        _log(f"{order_id}: refund call failed, will retry ({error})")
        counts["skipped"] += 1
        return

    if status == 200:
        try:
            states.transition(
                order_id,
                states.FAILED,
                states.REFUNDED,
                "reconciler",
                "Refund confirmed by gateway",
            )
        except states.StaleState:
            pass  # already marked refunded
        counts["refunded"] += 1
        return

    # 404: no payment exists, so there is nothing to refund. 409: not refundable.
    _log(f"{order_id}: refund answered {status}, leaving the order as FAILED")
    counts["skipped"] += 1


# ---------- Entry point ----------


def handler(event, context):
    now = int(time.time())
    counts = {
        "pending_recovered": 0,
        "pending_abandoned": 0,
        "token_recovered": 0,
        "redelivery_queued": 0,
        "failed": 0,
        "refunded": 0,
        "skipped": 0,
        "errors": 0,
    }

    work = [
        (states.PENDING, _seconds("STALE_PENDING_SECONDS", 300), None, _recover_pending),
        (states.PAID, _seconds("STALE_PAID_SECONDS", 300), None, _recover_paid),
        (
            states.FAILED,
            _seconds("REFUND_RETRY_SECONDS", 60),
            _seconds("LOOKBACK_SECONDS", 86400),
            lambda order, _now, c: _refund(order["order_id"], c),
        ),
    ]

    for status, min_age, max_age, action in work:
        for order in _find(status, min_age, now, max_age):
            if not _time_left(context):
                _log("running out of time, the rest waits for the next run")
                print(json.dumps({"reconciler": counts}))
                return counts
            try:
                action(order, now, counts)
            except Exception as error:  # noqa: BLE001 - one bad order must not stop the rest
                _log(f"{order.get('order_id')}: unexpected error: {error}")
                counts["errors"] += 1

    print(json.dumps({"reconciler": counts}))
    return counts
