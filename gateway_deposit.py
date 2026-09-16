"""
Deposit USDC into Circle Gateway on Arc, so a wallet can pay the Arc entry.

A Gateway nanopayment spends a balance that already sits in the GatewayWallet
contract. Filling it is the one on-chain step in the whole flow: approve, then
deposit. Every payment after that is a signature and costs no gas.

    DEPOSITOR_KEY=0x... python gateway_deposit.py --amount 1          # asks before sending
    python gateway_deposit.py --balance 0xYourAddress                 # read only

The key is read from the environment only. It never goes on the command line,
where it would land in shell history. Use a wallet that holds only what you are
about to deposit.

Do NOT send USDC to the GatewayWallet with a plain transfer: it is not credited.
Only deposit() counts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal

ARC_CHAIN_ID = 5042
ARC_NETWORK = f"eip155:{ARC_CHAIN_ID}"
ARC_RPC = os.getenv("ARC_RPC_URL", "https://rpc.mainnet.arc.io")
ARC_USDC = "0x3600000000000000000000000000000000000000"
GATEWAY_API = os.getenv("ARC_GATEWAY_API", "https://gateway-api.circle.com/v1")
GATEWAY_DOMAIN_ARC = 26           # Gateway's own chain id for Arc, not the EVM one
MIN_FEE_WEI = 20 * 10**9          # Arc drops anything under 20 gwei maxFeePerGas silently
MAX_DEPOSIT_UNITS = 10 * 10**6    # a guard, not a limit of Gateway: 10 USDC
# Circle Gateway answers 403 to urllib's default User-Agent.
HEADERS = {"Content-Type": "application/json", "User-Agent": "x402-paid-endpoint/1.1"}


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HEADERS,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rpc(method: str, params: list):
    out = post(ARC_RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in out:
        raise SystemExit(f"{method}: {out['error']}")
    return out["result"]


def gateway_balance(address: str) -> dict:
    out = post(f"{GATEWAY_API}/balances", {
        "token": "USDC", "sources": [{"domain": GATEWAY_DOMAIN_ARC, "depositor": address}]})
    return (out.get("balances") or [{}])[0]


def gateway_wallet() -> str:
    """The contract address as Gateway itself lists it for Arc, not a constant
    copied from a web page."""
    req = urllib.request.Request(f"{GATEWAY_API}/x402/supported", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        kinds = json.loads(r.read())["kinds"]
    for k in kinds:
        if k["network"] == ARC_NETWORK and k["extra"].get("name") == "GatewayWalletBatched":
            return k["extra"]["verifyingContract"]
    raise SystemExit("Gateway does not list Arc mainnet")


def word(value) -> str:
    """One ABI word: an address or an integer, left-padded to 32 bytes."""
    if isinstance(value, str):
        return value.lower().removeprefix("0x").rjust(64, "0")
    return format(value, "x").rjust(64, "0")


def send(acct, to: str, data: str) -> str:
    base_fee = int(rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    try:
        tip = int(rpc("eth_maxPriorityFeePerGas", []), 16)
    except SystemExit:
        tip = 10**9
    tx = {
        "type": 2, "chainId": ARC_CHAIN_ID,
        "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
        "to": to, "value": 0, "data": data,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": max(2 * base_fee + tip, MIN_FEE_WEI + tip),
    }
    est = int(rpc("eth_estimateGas", [{"from": acct.address, "to": to, "data": data}]), 16)
    tx["gas"] = est * 13 // 10
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
    for _ in range(60):
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if int(receipt["status"], 16) != 1:
                raise SystemExit(f"reverted: https://explorer.arc.io/tx/{tx_hash}")
            return tx_hash
        time.sleep(1)
    raise SystemExit(f"not mined after 60s: {tx_hash}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", help="USDC to deposit, e.g. 1 or 0.5")
    ap.add_argument("--balance", metavar="ADDRESS", help="only print the Gateway balance")
    args = ap.parse_args()

    if args.balance:
        print(json.dumps(gateway_balance(args.balance), indent=2))
        return 0
    if not args.amount:
        ap.error("--amount or --balance")

    key = os.getenv("DEPOSITOR_KEY")
    if not key:
        raise SystemExit("set DEPOSITOR_KEY in the environment")
    try:
        from eth_account import Account
        from eth_utils import keccak
    except ImportError:
        raise SystemExit("needs eth-account:  pip install eth-account")

    units = int(Decimal(args.amount) * 10**6)
    if not 0 < units <= MAX_DEPOSIT_UNITS:
        raise SystemExit("amount must be above 0 and at most 10 USDC")
    if int(rpc("eth_chainId", []), 16) != ARC_CHAIN_ID:
        raise SystemExit(f"{ARC_RPC} is not Arc mainnet")

    acct = Account.from_key(key)
    wallet = gateway_wallet()
    held = int(rpc("eth_call", [{"to": ARC_USDC, "data": "0x70a08231" + word(acct.address)},
                                "latest"]), 16)
    print(f"depositor:     {acct.address}")
    print(f"USDC on Arc:   {Decimal(held) / 10**6}")
    print(f"deposit:       {Decimal(units) / 10**6} USDC into GatewayWallet {wallet}")
    print(f"network:       Arc mainnet ({ARC_NETWORK}), gas is paid in USDC")
    if held <= units:
        raise SystemExit("not enough USDC: the deposit and the gas both come out of it")
    if input("type 'deposit' to send two transactions (approve, deposit): ").strip() != "deposit":
        print("nothing sent")
        return 0

    approve = "0x095ea7b3" + word(wallet) + word(units)
    print(f"approve:  https://explorer.arc.io/tx/{send(acct, ARC_USDC, approve)}")
    deposit = "0x" + keccak(text="deposit(address,uint256)")[:4].hex() + word(ARC_USDC) + word(units)
    print(f"deposit:  https://explorer.arc.io/tx/{send(acct, wallet, deposit)}")

    print("\nGateway credits the deposit once it has observed it; this can take a minute.")
    for _ in range(30):
        bal = gateway_balance(acct.address)
        if Decimal(bal.get("balance", "0")) > 0:
            print(json.dumps(bal, indent=2))
            return 0
        time.sleep(10)
    print(f"not credited yet; check later:  python gateway_deposit.py --balance {acct.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
