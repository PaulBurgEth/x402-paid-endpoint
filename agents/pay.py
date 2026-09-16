"""
An agent that pays for an endpoint it is not allowed to read for free.

    python agents/pay.py --url http://localhost:8402/quote --key 0x<private key>

It calls the endpoint until the free allowance runs out, reads the 402
challenge, signs an EIP-3009 transfer authorization, and retries the same
request with the signature attached. Nothing here talks to a chain: the payer
signs, the facilitator broadcasts. That is the point of the scheme — the payer
spends no gas, and the server never holds a key.

Use a throwaway key. A private key on a command line ends up in shell history.
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

HDR_REQUIRED = "payment-required"
HDR_SIGNATURE = "PAYMENT-SIGNATURE"
HDR_RESPONSE = "payment-response"


def b64(obj) -> str:
    return base64.b64encode(
        json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()


def unb64(value: str):
    return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))


def get(url: str, signature: str | None = None):
    # Named: urllib's default "Python-urllib/x.y" is refused by many bot filters.
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "base-usdc-payments/1.0"})
    if signature:
        req.add_header(HDR_SIGNATURE, signature)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def sign_base(accepted: dict, key: str) -> dict:
    """Sign TransferWithAuthorization over EIP-712.

    The domain comes out of the challenge's `extra`, never from a local
    constant: the name and version belong to the token contract, and wrong ones
    produce a signature that verifies against nothing while looking like a
    client bug.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        raise SystemExit("needs eth-account:  pip install eth-account")

    acct = Account.from_key(key)
    now = int(time.time())
    authorization = {
        "from": acct.address,
        "to": accepted["payTo"],
        "value": str(accepted["amount"]),
        # A generous window backwards: the payer's clock and the chain's drift,
        # and a validAfter in the future is the most annoying way to be refused.
        "validAfter": str(now - 600),
        "validBefore": str(now + int(accepted.get("maxTimeoutSeconds", 60)) + 600),
        "nonce": "0x" + secrets.token_hex(32),
    }
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
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"][2:]),
        },
    }
    signed = acct.sign_message(encode_typed_data(full_message=typed))
    sig = signed.signature.hex()
    print(f"  signed as {acct.address}")
    return {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {
            "signature": sig if sig.startswith("0x") else "0x" + sig,
            "authorization": authorization,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8402/quote")
    ap.add_argument("--key", help="payer private key; omit to stop at the challenge")
    ap.add_argument("--max-free", type=int, default=10)
    args = ap.parse_args()

    # 1. Spend the free allowance.
    for i in range(args.max_free):
        status, headers, body = get(args.url)
        if status == 402:
            print(f"call {i + 1}: 402 — allowance spent")
            break
        left = headers.get("x-ratelimit-remaining", "?")
        print(f"call {i + 1}: {status}, free calls left: {left}")
    else:
        print("still inside the free allowance; raise --max-free")
        return 0

    # 2. Read the challenge.
    raw = headers.get(HDR_REQUIRED)
    if not raw:
        print("402 without a PAYMENT-REQUIRED header: the paid rail is not configured")
        return 1
    ch = unb64(raw)
    accepted = ch["accepts"][0]
    print(f"\nchallenge: {accepted['amount']} units of {accepted['asset']}")
    print(f"           on {accepted['network']} to {accepted['payTo']}")
    print(f"           resource: {ch['resource']['url']}")
    if "extensions" in ch:
        print(f"           discovery: {json.dumps(ch['extensions']['bazaar']['info']['input'])}")

    if not args.key:
        print("\nno --key given, stopping at the challenge.")
        return 0

    # 3. Sign and retry the same request.
    payment = sign_base(accepted, args.key)
    payment["resource"] = ch["resource"]          # optional in the spec, required by facilitators
    status, headers, body = get(args.url, signature=b64(payment))
    print(f"\nretry with payment: {status}")
    if status != 200:
        print(body.decode()[:400])
        return 1

    receipt = headers.get(HDR_RESPONSE)
    if receipt:
        r = unb64(receipt)
        where = "DRY RUN, nothing settled" if r.get("dryRun") else f"tx {r.get('transaction')}"
        print(f"receipt: success={r.get('success')} payer={r.get('payer')} — {where}")
    print(f"body: {body.decode()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
