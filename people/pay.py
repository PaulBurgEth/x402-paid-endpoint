"""
Pay the checkout from a script, the way a person's wallet would: one ordinary
USDC transfer on Base. Useful to test the checkout without a phone.

    PAYER_KEY=0x... python people/pay.py --checkout http://localhost:8403 --price 0.10

Reads the amount and the address from the checkout's reservation, refuses to
send more than --max, sends an ERC-20 transfer (gas is paid in ETH), then asks
the checkout whether it noticed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import base  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", default="http://localhost:8403")
    ap.add_argument("--price", default="0.10")
    ap.add_argument("--max", default="1.00", help="refuse to send more than this many USDC")
    args = ap.parse_args()
    key = os.getenv("PAYER_KEY", "").strip()
    if not key:
        raise SystemExit("set PAYER_KEY in the environment")
    r = base.http_json(f"{args.checkout}/reserve", {"price": args.price})
    units, pay_to = int(r["units"]), r["pay_to"]
    print(f"checkout asks {r['amount']} USDC to {pay_to}\n  {r['uri']}")
    if Decimal(r["amount"]) > Decimal(args.max):
        raise SystemExit(f"refusing: more than --max {args.max}")
    tx = base.send_usdc(key, pay_to, units)
    print(f"sent  {base.EXPLORER}/tx/{tx}")
    for _ in range(12):
        req = urllib.request.Request(f"{args.checkout}/status/{r['id']}",
                                     headers={"User-Agent": base.USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = json.loads(resp.read())
        if status.get("paid"):
            print(f"checkout: paid, tx {status['tx']}")
            return 0
        time.sleep(5)
    print("checkout has not seen it yet; POST the hash to /confirm/<id>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
