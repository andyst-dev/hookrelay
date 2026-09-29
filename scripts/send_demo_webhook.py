"""Send the documented synthetic payment event to HookRelay."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json

import httpx

PAYLOAD = {
    "id": "evt_demo_001",
    "type": "payment.completed",
    "customer": {"email": "demo@example.com"},
    "amount": 4900,
    "currency": "CHF",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a synthetic HookRelay event")
    parser.add_argument("--url", default="http://localhost:8000/hooks/demo")
    parser.add_argument("--secret", help="Optional endpoint HMAC secret")
    args = parser.parse_args()
    body = json.dumps(PAYLOAD, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if args.secret:
        digest = hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-HookRelay-Signature"] = f"sha256={digest}"
    response = httpx.post(args.url, content=body, headers=headers, timeout=10)
    print(response.status_code, response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
