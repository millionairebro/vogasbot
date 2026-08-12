#!/usr/bin/env python3
"""
rh-sniper: a lean terminal FCFS mint sniper for Robinhood Chain (chain id 4663).

No Telegram. Smart auto-detect: it figures out the mint call for you.
  1) If the contract is verified -> reads the ABI, finds the mint fn + price.
  2) If unverified -> reads the raw bytecode and matches function selectors
     locally (no external database), recovering simple mints like mint(uint256).
  3) Builds calldata for every wallet automatically for SIMPLE / public mints.
  4) Detects a proof/signature mint and says so plainly (instead of a cryptic
     "failed to fetch ABI") - those need per-wallet calldata from the project.

You can always override by putting exact `calldata` in config.json; that wins.

Commands:
  python snipe.py --detect            # inspect a contract, print what it found
  python snipe.py --check             # simulate each wallet once, no fire
  python snipe.py                     # watch, fire all wallets when live
  python snipe.py --now               # fire immediately (sale already open)
"""
import os
import re
import sys
import json
import time
import asyncio
import argparse
import urllib.request
from decimal import Decimal
from datetime import datetime, timezone

from web3 import Web3
from eth_abi import encode as abi_encode


# ---- simple (auto-buildable) and proof (needs per-wallet calldata) signatures
SIMPLE_SIGS = [
    "mint(uint256)", "mint()", "mint(address,uint256)", "mint(uint256,address)",
    "publicMint(uint256)", "publicMint()", "mintPublic(uint256)", "mintPublic()",
    "mintTo(address,uint256)", "claim(uint256)", "claim()",
    "freeMint(uint256)", "freeMint()", "mint(address)",
]
PROOF_SIGS = [
    "mint(uint256,bytes32[])", "mint(bytes32[],uint256)", "mint(bytes32,bytes)",
    "mint(uint256,bytes)", "mintAllowList(uint256,bytes32[])",
    "whitelistMint(uint256,bytes32[])", "allowlistMint(uint256,bytes32[])",
    "mintWhitelist(uint256,bytes32[])", "claim(uint256,bytes32[])",
]


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")


def log(msg):
    print(f"{ts()}  {msg}", flush=True)


def to_wei_eth(x):
    return int(Web3.to_wei(Decimal(str(x or "0")), "ether"))


def norm_hex(cd):
    cd = (cd or "").strip()
    if cd and not cd.startswith("0x"):
        cd = "0x" + cd
    return cd


def parse_when(s):
    s = str(s or "").strip()
    if not s:
        return None
    if s.startswith("+"):
        parts = re.findall(r"(\d+)([dhms])", s[1:].lower())
        mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        return time.time() + sum(int(n) * mult[u] for n, u in parts)
    if re.fullmatch(r"\d{10}", s):
        return float(s)
    if re.fullmatch(r"\d{13}", s):
        return float(s) / 1000.0
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("start_time needs a timezone (Z for UTC, or an offset like +06:00)")
    return dt.timestamp()


def resolve_source(text):
    text = (text or "").strip()
    um = re.search(r"opensea\.io/(?:assets|item)/[^/]+/(0x[a-fA-F0-9]{40})", text)
    if um:
        return Web3.to_checksum_address(um.group(1))
    m = re.search(r"0x[a-fA-F0-9]{40}", text)
    if m:
        return Web3.to_checksum_address(m.group(0))
    raise ValueError("no contract address or OpenSea item link found")


# ---------------------------------------------------------------- detection
def selector(sig):
    return Web3.keccak(text=sig)[:4].hex().replace("0x", "").lower()


def sig_types(sig):
    inner = sig[sig.index("(") + 1:sig.rindex(")")]
    return [t for t in inner.split(",") if t]


def is_simple_types(types):
    if len(types) == 0:
        return True
    if len(types) == 1 and (types[0].startswith("uint") or types[0] == "address"):
        return True
    if len(types) == 2 and {types[0], types[1][:4]} <= {"address", "uint"}:
        return types[0] in ("address",) or types[0].startswith("uint")
    return False


def fetch_abi(blockscout, addr):
    url = f"{blockscout}?module=contract&action=getabi&address={addr}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode())
    if str(data.get("status")) == "1" and data.get("result"):
        return json.loads(data["result"])
    return None


def read_price(w3, addr, abi):
    getters = ("mintPrice", "price", "cost", "PRICE", "publicPrice", "getPrice", "tokenPrice")
    names = {i.get("name") for i in abi if i.get("type") == "function"}
    c = w3.eth.contract(address=addr, abi=abi)
    for g in getters:
        if g in names:
            try:
                v = c.functions[g]().call()
                if isinstance(v, int) and v >= 0:
                    return v
            except Exception:
                pass
    return None


def get_code(w3, addr):
    h = w3.eth.get_code(addr).hex()
    return h if h.startswith("0x") else "0x" + h


def detect(w3, addr, blockscout):
    """Return dict: source, kind ('simple'|'proof'|None), sig, types, price_wei, candidates."""
    out = {"source": None, "kind": None, "sig": None, "types": None,
           "price_wei": None, "candidates": []}
    # 1) verified ABI
    try:
        abi = fetch_abi(blockscout, addr)
    except Exception:
        abi = None
    if abi:
        out["source"] = "verified ABI"
        fns = []
        for i in abi:
            if i.get("type") == "function" and i.get("stateMutability") in ("payable", "nonpayable"):
                low = i.get("name", "").lower()
                if "mint" in low or "claim" in low:
                    fns.append((i["name"], [a["type"] for a in i.get("inputs", [])]))
        out["candidates"] = [f"{n}({','.join(t)})" for n, t in fns]
        try:
            out["price_wei"] = read_price(w3, addr, abi)
        except Exception:
            pass
        for n, types in sorted(fns, key=lambda x: (len(x[1]), x[0].lower() != "mint")):
            if is_simple_types(types):
                out.update(kind="simple", sig=f"{n}({','.join(types)})", types=types)
                return out
        if fns:
            n, types = fns[0]
            out.update(kind="proof", sig=f"{n}({','.join(types)})", types=types)
        return out
    # 2) unverified -> scan bytecode for known selectors
    code = get_code(w3, addr)
    if not code or code in ("0x", "0x0"):
        out["source"] = "no contract code at address"
        return out
    out["source"] = "bytecode selectors (unverified)"
    hexcode = code[2:].lower()
    found_simple = [s for s in SIMPLE_SIGS if ("63" + selector(s)) in hexcode]
    found_proof = [s for s in PROOF_SIGS if ("63" + selector(s)) in hexcode]
    out["candidates"] = found_simple + found_proof
    if found_simple:
        out.update(kind="simple", sig=found_simple[0], types=sig_types(found_simple[0]))
    elif found_proof:
        out.update(kind="proof", sig=found_proof[0], types=sig_types(found_proof[0]))
    return out


def build_calldata(sig, types, wallet_addr, qty):
    sel = selector(sig)
    if not types:
        return "0x" + sel
    args = []
    for t in types:
        if t.startswith("uint"):
            args.append(int(qty))
        elif t == "address":
            args.append(Web3.to_checksum_address(wallet_addr))
        else:
            raise ValueError(f"can't auto-fill arg type '{t}' - supply calldata manually")
    return "0x" + sel + abi_encode(types, args).hex()


# ---------------------------------------------------------------- tx engine
class Wallet:
    def __init__(self, name, acct, calldata, value_wei, qty):
        self.name = name
        self.acct = acct
        self.calldata = calldata
        self.value_wei = value_wei
        self.qty = qty
        self.raw = None
        self.nonce = None


async def run_blocking(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


def suggest_fees(w3):
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:
        base = None
    prio = w3.to_wei("0.01", "gwei")
    if base is None:
        return w3.eth.gas_price, prio
    return int(base) * 2 + prio, prio


def presign(w3, contract, chain_id, gas_limit, wal):
    nonce = w3.eth.get_transaction_count(wal.acct.address, "pending")
    max_fee, prio = suggest_fees(w3)
    try:
        gas = int(w3.eth.estimate_gas({
            "from": wal.acct.address, "to": contract, "data": wal.calldata, "value": wal.value_wei
        }) * 1.25)
    except Exception:
        gas = gas_limit
    tx = {
        "chainId": chain_id, "to": contract, "from": wal.acct.address, "data": wal.calldata,
        "value": wal.value_wei, "nonce": nonce, "gas": gas,
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
    }
    signed = w3.eth.account.sign_transaction(tx, wal.acct.key)
    wal.raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    wal.nonce = nonce
    return gas


def sim(w3, contract, wal):
    w3.eth.call({"from": wal.acct.address, "to": contract, "data": wal.calldata, "value": wal.value_wei})


def send(w3, raw, tries=2):
    last = None
    for _ in range(tries):
        try:
            h = w3.eth.send_raw_transaction(raw).hex()
            return h if h.startswith("0x") else "0x" + h
        except Exception as e:
            last = e
            es = str(e).lower()
            if "known" in es or "nonce too low" in es or "already" in es:
                raise  # already submitted/mined; don't double-send
    raise last


async def confirm(read_w3, explorer, wal, txh):
    try:
        r = await run_blocking(read_w3.eth.wait_for_transaction_receipt, txh, 90)
        total = Decimal(r.get("gasUsed", 0) * r.get("effectiveGasPrice", 0)) / Decimal(10**18)
        ok = r.get("status") == 1
        log(f"[{wal.name}] {'CONFIRMED' if ok else 'REVERTED'} | gas {r.get('gasUsed')} "
            f"| total {total:.8f} ETH | block {r.get('blockNumber')}")
    except Exception:
        log(f"[{wal.name}] not confirmed in 90s - check {explorer}/tx/{txh}")


async def fire_one(read_w3, submit_w3, explorer, wal):
    try:
        txh = await run_blocking(send, submit_w3, wal.raw)
        log(f"[{wal.name}] SENT {txh}")
        log(f"[{wal.name}] {explorer}/tx/{txh}")
        asyncio.create_task(confirm(read_w3, explorer, wal, txh))
    except Exception as e:
        log(f"[{wal.name}] fire failed: {str(e)[:140]}")


async def hunt_wallet(read_w3, submit_w3, contract, explorer, wal, interval, deadline):
    log(f"[{wal.name}] waiting for mint to go live...")
    while True:
        if deadline and time.time() > deadline:
            log(f"[{wal.name}] window closed - not live/eligible.")
            return
        try:
            await run_blocking(sim, read_w3, contract, wal)
        except Exception:
            await asyncio.sleep(interval)
            continue
        await fire_one(read_w3, submit_w3, explorer, wal)
        return


async def shared_gate(read_w3, submit_w3, contract, explorer, wals, interval, deadline):
    rep = wals[0]
    log(f"watching for open (shared gate, {len(wals)} wallets)...")
    while True:
        if deadline and time.time() > deadline:
            log("window closed - mint never opened.")
            return
        try:
            await run_blocking(sim, read_w3, contract, rep)
        except Exception:
            await asyncio.sleep(interval)
            continue
        log("MINT OPEN - firing all wallets")
        await asyncio.gather(*[fire_one(read_w3, submit_w3, explorer, w) for w in wals])
        return


# ---------------------------------------------------------------- main
async def amain(args):
    cfg = json.load(open(args.config)) if os.path.exists(args.config) else {}
    rpc = cfg.get("rpc_url") or ""
    if not rpc:
        log("Set rpc_url in config.json.")
        sys.exit(1)
    submit = cfg.get("submit_rpc_url") or rpc
    chain_id = int(cfg.get("chain_id", 4663))
    contract = resolve_source(args.contract or cfg.get("contract", ""))
    explorer = cfg.get("explorer", "https://robinhoodchain.blockscout.com").rstrip("/")
    blockscout = cfg.get("blockscout_api", "https://robinhoodchain.blockscout.com/api")
    interval = max(0.05, float(cfg.get("poll_ms", 250)) / 1000.0)
    gas_limit = int(cfg.get("gas_limit", 500000))
    default_qty = int(cfg.get("qty", 1))
    global_cd = norm_hex(args.calldata or cfg.get("calldata"))
    global_val = to_wei_eth(cfg.get("value_eth", "0"))

    read_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
    submit_w3 = Web3(Web3.HTTPProvider(submit, request_kwargs={"timeout": 10}))

    try:
        bn = await run_blocking(lambda: read_w3.eth.block_number)
    except Exception as e:
        log(f"RPC error: {e}")
        sys.exit(1)

    # --detect: inspect the contract and print what to do, no keys needed
    if args.detect:
        d = await run_blocking(detect, read_w3, contract, blockscout)
        log(f"Contract {contract} | block {bn}")
        log(f"Detected via: {d['source']}")
        if d["candidates"]:
            log("mint candidates: " + ", ".join(d["candidates"]))
        if d["price_wei"] is not None:
            log(f"price on-chain: {Decimal(d['price_wei'])/Decimal(10**18)} ETH")
        if d["kind"] == "simple":
            log(f"=> SIMPLE mint {d['sig']} - leave calldata blank, the script auto-builds for all wallets.")
        elif d["kind"] == "proof":
            log(f"=> PROOF/whitelist mint {d['sig']} - each wallet needs its own 'calldata' from the project.")
        else:
            log("=> couldn't auto-detect. Capture calldata from a real mint tx and set it in config.")
        return

    # build wallets
    acctf = Web3().eth.account
    raw_wallets = cfg.get("wallets", [])
    wals = []
    detected = None
    for i, w in enumerate(raw_wallets):
        key = (w.get("key") or "").strip()
        if not key:
            continue
        name = w.get("name") or f"w{i+1}"
        qty = int(w.get("qty", default_qty))
        acct = acctf.from_key(key)
        cd = norm_hex(w.get("calldata")) or global_cd
        if not cd:
            if detected is None:
                log("No calldata set - auto-detecting the mint call...")
                detected = await run_blocking(detect, read_w3, contract, blockscout)
                log(f"detect: {detected['source']} | candidates: {', '.join(detected['candidates']) or 'none'}")
                if detected["kind"] == "proof":
                    log(f"This is a PROOF/whitelist mint ({detected['sig']}). Each wallet needs its own "
                        "'calldata' from the project's mint page. No bot can invent it.")
                    sys.exit(1)
                if detected["kind"] != "simple":
                    log("Could not auto-detect a simple mint. Capture calldata from a real mint tx "
                        "and put it in config.json (global or per-wallet).")
                    sys.exit(1)
                if detected["price_wei"] and not global_val:
                    log(f"using on-chain price {Decimal(detected['price_wei'])/Decimal(10**18)} ETH")
            cd = build_calldata(detected["sig"], detected["types"], acct.address, qty)
        val = to_wei_eth(w["value_eth"]) if w.get("value_eth") else global_val
        if not val and detected and detected.get("price_wei"):
            val = detected["price_wei"] * qty
        wals.append(Wallet(name, acct, cd, val, qty))

    if not wals:
        log("No wallets in config.")
        sys.exit(1)

    log(f"Contract {contract} | chain {chain_id} | {len(wals)} wallet(s) | poll {int(interval*1000)}ms | block {bn}")
    if detected and detected["kind"] == "simple":
        log(f"auto-built calldata from {detected['sig']} ({detected['source']})")

    # --check: simulate once, no fire
    if args.check:
        async def chk(wal):
            try:
                await run_blocking(sim, read_w3, contract, wal)
                log(f"[{wal.name}] ELIGIBLE now - would mint")
            except Exception as e:
                log(f"[{wal.name}] not yet / not eligible: {str(e)[:100]}")
        await asyncio.gather(*[chk(w) for w in wals])
        return

    start = None if args.now else parse_when(cfg.get("start_time", ""))
    if start:
        lead = float(cfg.get("lead_seconds", 3))
        log(f"Scheduled {datetime.fromtimestamp(start, tz=timezone.utc):%Y-%m-%d %H:%M:%S UTC}; arming {int(lead)}s early")
        while time.time() < start - lead:
            await asyncio.sleep(min(start - lead - time.time(), 30))

    for wal in wals:
        try:
            g = await run_blocking(presign, read_w3, contract, chain_id, gas_limit, wal)
            log(f"[{wal.name}] armed nonce {wal.nonce} gas {g}")
        except Exception as e:
            log(f"[{wal.name}] arm failed: {str(e)[:140]}")
    try:
        await run_blocking(lambda: submit_w3.eth.block_number)  # warm submit socket
    except Exception:
        pass

    deadline = (start + float(cfg.get("window_seconds", 180))) if start else None

    if args.now:
        await asyncio.gather(*[fire_one(read_w3, submit_w3, explorer, w) for w in wals])
    else:
        # efficient path: if every wallet shares the same call, watch one gate then fire all
        shared = len({w.calldata for w in wals}) == 1 and len({w.value_wei for w in wals}) == 1
        if shared and len(wals) > 1:
            await shared_gate(read_w3, submit_w3, contract, explorer, wals, interval, deadline)
        else:
            await asyncio.gather(*[
                hunt_wallet(read_w3, submit_w3, contract, explorer, w, interval, deadline) for w in wals
            ])

    await asyncio.sleep(5)  # let confirmations print


def main():
    ap = argparse.ArgumentParser(description="Robinhood Chain terminal mint sniper")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--contract", default="")
    ap.add_argument("--calldata", default="")
    ap.add_argument("--detect", action="store_true", help="inspect the contract's mint fn and exit")
    ap.add_argument("--check", action="store_true", help="simulate each wallet once and exit (no fire)")
    ap.add_argument("--now", action="store_true", help="fire immediately (sale already open)")
    args = ap.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
