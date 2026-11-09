"""
Part of the MOCK gateway: takes callbacks from the outbox queue and POSTs them
to our callback endpoint, signed with a shared secret (like a real gateway
signing its result-URL calls).

Like a real gateway, it retries when our endpoint is unavailable: a failed
message goes back on the queue and is tried again, and eventually lands in the
dead-letter queue.
"""

import os

from ttt import common


def handler(event, context):
    secret = common.get_secret()
    url = os.environ["CALLBACK_URL"]
    failures = []

    for record in event["Records"]:
        body = record["body"]
        headers = {
            "Content-Type": "application/json",
            "x-gateway-signature": common.sign(body, secret),
        }
        try:
            status, _ = common.http_request("POST", url, body, headers, timeout=5)
            if status == 429 or status >= 500:
                raise common.TransientError(f"callback endpoint returned {status}")
            # Other 4xx responses are a final answer: retrying cannot help.
        except Exception as error:  # noqa: BLE001 - any failure means "retry later"
            print(f"callback failed, will retry: {error}")
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}
