"""What the people's checkout needs to know about Base. Standard library only.

USDC on Base is an ERC-20 at 0x8335...2913 with 6 decimals. A transfer logs one
`Transfer(from, to, value)` from that contract; gas is paid in ETH.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from decimal import Decimal

CHAIN_ID = 8453
RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EXPLORER = "https://basescan.org"
USER_AGENT = "base-usdc-payments/1.0"         # urllib's default is refused by many filters


def http_json(url: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rpc(method: str, params: list):
    out = http_json(RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in out:
        raise RuntimeError(f"base {method}: {out['error'].get('message', out['error'])}")
    return out["result"]


def word(value) -> str:
    if isinstance(value, str):
        return value.lower().removeprefix("0x").rjust(64, "0")
    return format(value, "x").rjust(64, "0")


def human(units: int) -> str:
    return f"{Decimal(units) / 10**6:f} USDC"


def transfer_from_log(log: dict, pay_to: str) -> dict | None:
    """A USDC Transfer into `pay_to`, or None. Only logs emitted by the USDC
    contract count: any contract can emit an event called Transfer."""
    try:
        topics = log["topics"]
        if (log["address"].lower() != USDC.lower() or topics[0] != TRANSFER_TOPIC
                or topics[2].lower() != "0x" + word(pay_to)):
            return None
        return {"tx_hash": log.get("transactionHash", ""), "units": int(log["data"], 16),
                "from": "0x" + topics[1][-40:], "block": int(log.get("blockNumber", "0x0"), 16)}
    except (KeyError, IndexError, ValueError, AttributeError):
        return None


def transfers_to(pay_to: str, from_block: int, to_block: int) -> list:
    """mainnet.base.org refuses very wide ranges; callers ask for a few hundred
    blocks (a Base block is 2 s)."""
    logs = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block), "address": USDC,
                                "topics": [TRANSFER_TOPIC, None, "0x" + word(pay_to)]}]) or []
    return [t for t in (transfer_from_log(log, pay_to) for log in logs) if t]


def send_usdc(key: str, to: str, units: int) -> str:
    """ERC-20 transfer, signed locally, gas in ETH. Waits for the receipt."""
    from eth_account import Account
    from eth_utils import to_checksum_address

    acct = Account.from_key(key)
    data = "0xa9059cbb" + word(to) + word(units)
    base_fee = int(rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    tip = 1_000_000
    tx = {"type": 2, "chainId": CHAIN_ID, "to": to_checksum_address(USDC), "value": 0, "data": data,
          "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
          "maxPriorityFeePerGas": tip, "maxFeePerGas": 2 * base_fee + tip}
    tx["gas"] = int(rpc("eth_estimateGas", [{"from": acct.address, "to": USDC, "data": data}]), 16) * 14 // 10
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
    for _ in range(60):
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if int(receipt["status"], 16) != 1:
                raise RuntimeError(f"reverted: {EXPLORER}/tx/{tx_hash}")
            return tx_hash
        time.sleep(4)
    raise RuntimeError(f"not mined in 4 minutes: {EXPLORER}/tx/{tx_hash}")
