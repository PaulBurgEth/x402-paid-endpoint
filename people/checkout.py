"""
A USDC checkout for people, on Base: scan a QR, pay from any wallet, the page notices.

    PAY_TO=0xYourAddress python people/checkout.py
    open http://localhost:8403/

    POST /reserve   {"price": "0.10"}   -> {id, amount, uri, pay_to, expires_at}
    GET  /status/<id>                   -> {paid, tx}   reads Base, grants once
    POST /confirm/<id> {"tx": "0x..."}  -> {paid, tx}   for a payer who closed the page

No wallet connection, no processor, no key on the server. The payer's wallet
sends an ordinary USDC transfer; the server only reads the chain.

THE AMOUNT IS THE IDENTIFIER. Each reservation gets a price no other open
reservation holds (walking up a cent at a time), so a transfer is matched by its
exact value. No memo to forget, no per-payment address to derive.

THE QR IS EIP-681 WITH THE CHAIN ID. `ethereum:<USDC>@8453/transfer?...` makes a
wallet switch to Base; USDC sent to this address on another chain is the classic
unrecoverable mistake.

Standard library only. `pip install segno` draws the QR.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import base  # noqa: E402

PORT = int(os.getenv("PORT", "8403"))
PAY_TO = os.getenv("PAY_TO", "")
WINDOW_SECONDS = 15 * 60
CENT = 10_000                     # one cent in 6-decimal units
SLOTS = 60
# A Base block is 2 s: 450 blocks cover the 15-minute window, and a public
# node serves a range that size.
POLL_BLOCKS = 450

_lock = threading.Lock()
_reservations: dict = {}          # id -> {units, expires, paid_tx}
_used_tx: set = set()             # one transfer pays one reservation


def eip681_uri(units: int) -> str:
    """ERC-20 transfer of USDC on Base, integer units (6 decimals)."""
    return f"ethereum:{base.USDC}@{base.CHAIN_ID}/transfer?address={PAY_TO}&uint256={units}"


def reserve(price: Decimal) -> dict:
    base = int(price * 10**6)
    now = time.time()
    with _lock:
        taken = {r["units"] for r in _reservations.values() if r["expires"] > now and not r["paid_tx"]}
        for step in range(SLOTS):
            units = base + step * CENT
            if units not in taken:
                rid = uuid.uuid4().hex[:12]
                _reservations[rid] = {"units": units, "expires": now + WINDOW_SECONDS, "paid_tx": ""}
                return {"id": rid, "amount": f"{Decimal(units) / 10**6:f}", "units": units,
                        "uri": eip681_uri(units), "pay_to": PAY_TO,
                        "expires_at": int(now + WINDOW_SECONDS)}
    raise RuntimeError("every amount slot is taken")


def _settle(rid: str, transfer: dict) -> bool:
    with _lock:
        r = _reservations[rid]
        if r["paid_tx"]:
            return True
        if transfer["tx_hash"] in _used_tx or transfer["units"] != r["units"]:
            return False
        r["paid_tx"] = transfer["tx_hash"]
        _used_tx.add(transfer["tx_hash"])
    print(f"  PAID {rid}: {base.human(transfer['units'])} from {transfer['from']} "
          f"{base.EXPLORER}/tx/{transfer['tx_hash']}")
    return True


def check(rid: str) -> dict:
    r = _reservations[rid]
    if not r["paid_tx"]:
        latest = int(base.rpc("eth_blockNumber", []), 16)
        for t in base.transfers_to(PAY_TO, max(latest - POLL_BLOCKS, 0), latest):
            if _settle(rid, t):
                break
    return {"paid": bool(r["paid_tx"]), "tx": r["paid_tx"]}


def confirm(rid: str, tx_hash: str) -> dict:
    """The hash is user input: read the receipt, trust nothing in the request."""
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash or ""):
        receipt = base.rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt and receipt.get("status") == "0x1":
            for log in receipt.get("logs") or []:
                t = base.transfer_from_log({**log, "transactionHash": tx_hash}, PAY_TO)
                if t and _settle(rid, t):
                    break
    r = _reservations[rid]
    return {"paid": bool(r["paid_tx"]), "tx": r["paid_tx"]}


PAGE = """<!doctype html><meta charset=utf-8><title>Pay in USDC on Base</title>
<body style="font-family:system-ui;max-width:32rem;margin:2rem auto;padding:0 1rem">
<h1>Pay in USDC on Base</h1><button id=b>Show code for $0.10</button>
<div id=o hidden><div id=qr></div>
<p>Send exactly <b id=amt></b> USDC on Base to <code id=to></code></p>
<p><a id=link>open in wallet</a></p><p id=st>waiting for the transfer…</p></div>
<script>
b.onclick=async()=>{
  const r=await(await fetch('/reserve',{method:'POST',body:'{"price":"0.10"}'})).json();
  // The QR is an SVG drawn by this server from its own payment link, the only markup inserted.
  qr.innerHTML=r.qr||'';
  amt.textContent=r.amount; to.textContent=r.pay_to; link.href=r.uri; o.hidden=false;
  const t=setInterval(async()=>{
    const s=await(await fetch('/status/'+r.id)).json();
    if(s.paid){clearInterval(t); st.textContent='paid: '+s.tx;}
  },5000);
};
</script>"""


def qr_svg(uri: str) -> str:
    try:
        import io

        import segno
    except ImportError:
        return ""
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=3, xmldecl=False,
                                    dark="#000000", light="#ffffff")
    return buf.getvalue().decode()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/":
            raw = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        m = re.fullmatch(r"/status/([0-9a-f]{12})", self.path)
        if m and m.group(1) in _reservations:
            try:
                return self._json(200, check(m.group(1)))
            except Exception as e:  # a flaky node must not break the page
                return self._json(200, {"paid": False, "tx": "", "note": f"chain unreadable: {e}"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/reserve":
            try:
                price = Decimal(str(self._body().get("price", "0.10")))
                if not Decimal("0.01") <= price <= Decimal("1000"):
                    raise ValueError
            except (ValueError, ArithmeticError):
                return self._json(400, {"error": "price must be between 0.01 and 1000"})
            r = reserve(price)
            return self._json(200, {**r, "qr": qr_svg(r["uri"])})
        m = re.fullmatch(r"/confirm/([0-9a-f]{12})", self.path)
        if m and m.group(1) in _reservations:
            return self._json(200, confirm(m.group(1), str(self._body().get("tx", ""))))
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    if not PAY_TO:
        raise SystemExit("set PAY_TO to the address that receives payments")
    print(f"USDC checkout on Base: http://localhost:{PORT}/  paying to {PAY_TO}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
