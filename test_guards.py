"""
What the endpoint refuses. Run the server first:

    X402_PAY_TO=0x000000000000000000000000000000000000dEaD \
    FREE_CALLS_PER_DAY=1 X402_CALLS_PER_PAYMENT=1 python server.py

then:

    python test_guards.py
"""
from __future__ import annotations

import json
import secrets
import sys

import agent as A

URL = "http://localhost:8402/quote"


def to_402() -> dict:
    for _ in range(50):
        status, headers, _ = A.get(URL)
        if status == 402:
            return headers
    raise SystemExit("never reached 402 — is FREE_CALLS_PER_DAY small enough?")


def main() -> int:
    headers = to_402()
    accepted = A.unb64(headers["payment-required"])["accepts"][0]
    key = "0x" + secrets.token_hex(32)   # throwaway, holds nothing

    def attempt(label: str, mutate=None, raw=None) -> tuple[int, str]:
        to_402()
        if raw is not None:
            status, _, body = A.get(URL, signature=raw)
        else:
            payment = A.sign_base(accepted, key)
            if mutate:
                mutate(payment["payload"]["authorization"])
            status, _, body = A.get(URL, signature=A.b64(payment))
        return status, json.loads(body).get("error", "")

    good = A.sign_base(accepted, key)
    signature = A.b64(good)
    status, _, _ = A.get(URL, signature=signature)
    checks = [("a valid payment is accepted", status, 200, "")]

    to_402()
    status, _, body = A.get(URL, signature=signature)
    checks.append(("the same authorization cannot be replayed", status, 402,
                   json.loads(body).get("error", "")))
    checks.append(("the wrong amount is refused",
                   *attempt("amount", lambda a: a.update(value="1")), ))
    checks.append(("a payment to someone else is refused",
                   *attempt("payTo", lambda a: a.update(
                       to="0x0000000000000000000000000000000000000001")), ))
    checks.append(("a signature that does not match the payer is refused",
                   *attempt("payer", lambda a: a.update(
                       **{"from": "0x0000000000000000000000000000000000000002"})), ))
    checks.append(("a malformed header is a 402, not a 500",
                   *attempt("garbage", raw="not-base64-json"), ))

    failed = 0
    for row in checks:
        label, status = row[0], row[1]
        expected = 200 if "accepted" in label else 402
        note = row[3] if len(row) > 3 else (row[2] if isinstance(row[2], str) else "")
        ok = status == expected
        failed += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {label}: {status} {note}")
    print("\n" + ("PASS" if not failed else f"{failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
