"""
A paid HTTP endpoint that speaks x402 v2. Standard library only.

The endpoint is free up to a daily allowance per caller. Past that line it
answers 402 with a payment challenge instead of a flat refusal: the caller signs
an EIP-3009 transfer authorization, sends it back on the next request, and the
same request goes through.

    GET /quote                      -> 200 while the caller has free calls left
    GET /quote  (allowance spent)   -> 402 + PAYMENT-REQUIRED header
    GET /quote  + PAYMENT-SIGNATURE -> 200 + PAYMENT-RESPONSE header (tx hash)

Run:
    python server.py                # dry-run: signatures verified locally
    FACILITATOR_URL=... FACILITATOR_API_KEY=... python server.py   # settles

Extracted from the agent layer of helprentdanang.com, reduced to one endpoint.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

X402_VERSION = 2
HDR_REQUIRED = "PAYMENT-REQUIRED"    # our 402 carries this
HDR_SIGNATURE = "PAYMENT-SIGNATURE"  # the client retries with this
HDR_RESPONSE = "PAYMENT-RESPONSE"    # our 200 carries this

# --- configuration -----------------------------------------------------------
PORT = int(os.getenv("PORT", "8402"))
FREE_CALLS_PER_DAY = int(os.getenv("FREE_CALLS_PER_DAY", "3"))
NETWORK = os.getenv("X402_NETWORK", "eip155:8453")                     # Base
ASSET = os.getenv("X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")  # USDC
ASSET_NAME = os.getenv("X402_ASSET_NAME", "USD Coin")
ASSET_VERSION = os.getenv("X402_ASSET_VERSION", "2")
PAY_TO = os.getenv("X402_PAY_TO", "0x0000000000000000000000000000000000000000")
PRICE_UNITS = os.getenv("X402_PRICE_UNITS", "10000")   # 0.01 USDC, 6 decimals
CALLS_PER_PAYMENT = int(os.getenv("X402_CALLS_PER_PAYMENT", "5000"))
TIMEOUT_SECONDS = int(os.getenv("X402_TIMEOUT_SECONDS", "60"))
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "")
FACILITATOR_API_KEY = os.getenv("FACILITATOR_API_KEY", "")

# In-memory and therefore not production. A real deployment keeps allowance and
# credit in a store that survives a restart and is shared across workers.
_used_today = defaultdict(int)
_credit = defaultdict(int)
_spent_nonces = set()


def b64(obj) -> str:
    """Header values are base64 of compact JSON.

    b64encode does not wrap lines; encodebytes does, and a newline inside a
    header value is a request-splitting bug rather than a formatting quirk.
    """
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(raw).decode()


def unb64(value: str):
    """Decode a header the client sent. None on anything malformed.

    Padding is repaired rather than rejected: some clients pad, some do not, and
    being strict here turns otherwise perfect payments into mysterious refusals.
    A caller must read None as "no payment", never as a server error.
    """
    try:
        return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    except Exception:
        return None


def requirements(resource_url: str) -> dict:
    """What we accept for this resource on an EVM chain."""
    return {
        "scheme": "exact",
        "network": NETWORK,
        # A string, because the spec says so and because six-decimal USDC in a
        # float is a rounding bug waiting for the first amount ending in a 5.
        "amount": str(int(PRICE_UNITS)),
        "asset": ASSET,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": TIMEOUT_SECONDS,
        # `extra` carries the EIP-712 domain: for USDC, the token contract's own
        # name and version. Get these wrong and the client produces a signature
        # that verifies against nothing, which looks like the client's fault.
        "extra": {"name": ASSET_NAME, "version": ASSET_VERSION},
    }


def bazaar_block() -> dict:
    """Discovery. There is no /.well-known/x402 in the specification, so a paid
    endpoint describes its own call shape inside the 402 and facilitators
    catalogue it from there. Without this a facilitator that finds the challenge
    learns the price and nothing about how to call us."""
    return {
        "info": {
            "input": {
                "type": "http",
                "method": "GET",
                "queryParams": {"district": "string, optional"},
            },
            "output": {"type": "application/json"},
        }
    }


def challenge(resource_url: str, error: str = "") -> str:
    return b64({
        "x402Version": X402_VERSION,
        "error": error or f"{HDR_SIGNATURE} header is required",
        "resource": {
            "url": resource_url,
            "description": "Median rent quote for one district.",
            "mimeType": "application/json",
        },
        "accepts": [requirements(resource_url)],
        "extensions": {"bazaar": bazaar_block()},
    })


class Refused(Exception):
    pass


def _facilitator(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        FACILITATOR_URL.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {FACILITATOR_API_KEY}"} if FACILITATOR_API_KEY else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Refused(f"{path} -> {e.code} {e.read()[:200]!r}") from e


def verify_locally(payment: dict) -> str:
    """Dry run: recover the signer from the EIP-712 signature and check the
    authorization matches what we asked for. Proves the protocol round trip
    without moving money. It does NOT check the payer's balance or allowance,
    and it does not spend the authorization on chain."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    auth = payment["payload"]["authorization"]
    accepted = payment["accepted"]
    if auth["to"].lower() != PAY_TO.lower():
        raise Refused("authorization pays someone else")
    if str(auth["value"]) != str(int(PRICE_UNITS)):
        raise Refused("authorization is for the wrong amount")
    now = int(time.time())
    if not (int(auth["validAfter"]) <= now < int(auth["validBefore"])):
        raise Refused("authorization is outside its validity window")
    if auth["nonce"] in _spent_nonces:
        raise Refused("authorization nonce already used")

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": accepted["extra"]["name"],
            "version": accepted["extra"]["version"],
            "chainId": int(accepted["network"].split(":")[1]),
            "verifyingContract": accepted["asset"],
        },
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:]),
        },
    }
    signer = Account.recover_message(
        encode_typed_data(full_message=typed), signature=payment["payload"]["signature"]
    )
    if signer.lower() != auth["from"].lower():
        raise Refused("signature does not match the stated payer")
    _spent_nonces.add(auth["nonce"])
    return signer


def settle(payment: dict, resource_url: str) -> dict:
    """Verify, then settle. Two calls on purpose: settling a payload that would
    not verify burns the facilitator's gas for an error nobody can explain
    afterwards."""
    if not FACILITATOR_URL:
        signer = verify_locally(payment)
        return {"success": True, "transaction": "", "payer": signer, "dryRun": True}

    body = {"x402Version": X402_VERSION,
            "paymentPayload": payment,
            "paymentRequirements": requirements(resource_url)}
    verdict = _facilitator("/verify", body)
    if not verdict.get("isValid"):
        raise Refused(f"facilitator rejected the payment: {verdict.get('invalidReason')}")
    return _facilitator("/settle", body)


def response_header(settlement: dict) -> str:
    return b64({
        "x402Version": X402_VERSION,
        "success": bool(settlement.get("success")),
        "transaction": settlement.get("transaction", ""),
        "network": NETWORK,
        "payer": settlement.get("payer", ""),
        **({"dryRun": True} if settlement.get("dryRun") else {}),
    })


PAYLOAD = {
    "district": "Son Tra",
    "median_rent_vnd": 9_500_000,
    "sample": 214,
    "note": "Illustrative figures. This repository is a protocol example.",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "x402-example"

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: dict, extra_headers: dict = None):
        raw = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.path.startswith("/quote"):
            return self._send(404, {"error": "try /quote"})

        caller = self.client_address[0]
        resource_url = f"http://localhost:{PORT}/quote"
        remaining = FREE_CALLS_PER_DAY - _used_today[caller]

        # 1. Still inside the free allowance.
        if remaining > 0:
            _used_today[caller] += 1
            return self._send(200, PAYLOAD, {
                "X-RateLimit-Limit": str(FREE_CALLS_PER_DAY),
                "X-RateLimit-Remaining": str(remaining - 1),
            })

        # 2. Paid credit bought by an earlier payment.
        if _credit[caller] > 0:
            _credit[caller] -= 1
            return self._send(200, PAYLOAD, {"X-Paid-Calls-Remaining": str(_credit[caller])})

        # 3. A payment was presented with this request.
        raw = self.headers.get(HDR_SIGNATURE)
        if raw:
            payment = unb64(raw)
            if payment is None:
                return self._send(402, {"error": f"{HDR_SIGNATURE} is not base64 JSON"},
                                  {HDR_REQUIRED: challenge(resource_url, "malformed payment")})
            try:
                settlement = settle(payment, resource_url)
            except Refused as e:
                return self._send(402, {"error": str(e)},
                                  {HDR_REQUIRED: challenge(resource_url, str(e))})
            _credit[caller] += CALLS_PER_PAYMENT - 1
            return self._send(200, PAYLOAD, {
                HDR_RESPONSE: response_header(settlement),
                "X-Paid-Calls-Remaining": str(_credit[caller]),
            })

        # 4. Allowance spent, nothing paid: challenge rather than refuse.
        return self._send(402, {"error": "payment required", "price_units": str(int(PRICE_UNITS)),
                                "calls_per_payment": CALLS_PER_PAYMENT},
                          {HDR_REQUIRED: challenge(resource_url)})


if __name__ == "__main__":
    mode = "settling via facilitator" if FACILITATOR_URL else "DRY RUN (signatures verified locally, no settlement)"
    print(f"x402 example server on http://localhost:{PORT}/quote")
    print(f"  free allowance: {FREE_CALLS_PER_DAY} calls per caller")
    print(f"  price: {PRICE_UNITS} units of {ASSET} on {NETWORK} for {CALLS_PER_PAYMENT} calls")
    print(f"  mode: {mode}\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
