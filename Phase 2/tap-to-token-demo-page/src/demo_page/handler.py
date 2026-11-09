"""
GET /

Serves the single-page demo (index.html). The page talks to the same API it
is served from, so no cross-site access is needed. This function has no AWS
permissions beyond writing its own logs.
"""

import os

_PAGE = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The page only runs its own inline script and only calls its own API.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def handler(event, context):
    return {"statusCode": 200, "headers": HEADERS, "body": _PAGE}
