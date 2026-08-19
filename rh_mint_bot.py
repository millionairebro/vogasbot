#!/usr/bin/env python3
"""
Robinhood Chain MULTI-WALLET NFT mint bot (Telegram-controlled).

YOU run this yourself. Private keys are read from a local wallets.json (or a
single RH_PRIVATE_KEY) on YOUR machine and never leave it. Telegram is only a
remote trigger.

Robinhood Chain mainnet: chain id 4663, gas token ETH, FIRST-COME-FIRST-SERVED
sequencing -> higher gas does NOT win, lowest latency wins. This bot pre-signs
each wallet's tx and fires the instant that wallet is eligible + the sale is open.

WHAT IT AUTOMATES:
  - resolve a contract from an OpenSea item link or a raw 0x address
  - fetch the verified ABI (Blockscout), auto-detect the mint function + price
  - build calldata for every wallet automatically (SIMPLE / public mints)
  - check each wallet's eligibility by simulation, fire per wallet when open

WHAT IT CANNOT DO (no bot can): invent a Merkle proof or a signature for a
proof-gated whitelist. Those come from the project per wallet -> put each in
wallets.json under "calldata". The bot flags which case you're in via /source.
"""

import os
import re
import json
import time
import asyncio
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3
from eth_abi import encode as abi_encode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("rh-mint-bot")
load_dotenv()

# ------------------------------------------------------------------ config
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID       = int(os.environ.get("AUTHORIZED_USER_ID", "0") or "0")
PRIVATE_KEY    = os.environ.get("RH_PRIVATE_KEY", "").strip()  # fallback if no wallets.json
WALLETS_FILE   = os.environ.get("RH_WALLETS_FILE", "wallets.json").strip()
RPC_URL        = os.environ.get("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com").strip()
SUBMIT_RPC_URL = (os.environ.get("RH_SUBMIT_RPC_URL", "").strip() or RPC_URL)
_pool_raw = os.environ.get("RH_RPC_POOL", "").strip()
RPC_POOL = [u.strip() for u in re.split(r"[,\s]+", _pool_raw) if u.strip()] or [RPC_URL]
RPC_POOL_RAW = list(RPC_POOL) + [RPC_URL, SUBMIT_RPC_URL]
MAX_SPEND_ETH = Decimal(os.environ.get("RH_MAX_SPEND_ETH", "0.5") or "0.5")
CHAIN_ID       = int(os.environ.get("RH_CHAIN_ID", "4663") or "4663")
BLOCKSCOUT     = os.environ.get("RH_BLOCKSCOUT_API", "https://robinhoodchain.blockscout.com/api").strip()
EXPLORER       = os.environ.get("RH_EXPLORER", "https://robinhoodchain.blockscout.com").rstrip("/")
DEFAULT_GAS    = int(os.environ.get("RH_GAS_LIMIT", "500000") or "500000")
OPENSEA_KEY    = os.environ.get("OPENSEA_API_KEY", "").strip()  # optional, for collection links

PK_PATTERN = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")

# --- secret redaction -------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def redact_url(url):
    """Keep the host so errors stay diagnosable; drop the path (holds the API key)."""
    try:
        m = re.match(r"(https?://[^/]+)", url)
        return (m.group(1) + "/***") if m else "***"
    except Exception:
        return "***"


def safe(text, limit=140):
    """Strip API keys / URLs / key-like tokens before anything reaches Telegram or logs."""
    s = str(text)
    s = _URL_RE.sub(lambda m: redact_url(m.group(0)), s)
    s = PK_PATTERN.sub("***", s)
    if BOT_TOKEN:
        s = s.replace(BOT_TOKEN, "***")
    for _u in RPC_POOL_RAW:
        tail = _u.rstrip("/").rsplit("/", 1)[-1]
        if len(tail) >= 12:
            s = s.replace(tail, "***")
    return s[:limit]

# simple (auto-buildable) vs proof (needs per-wallet calldata) mint signatures
SIMPLE_SIGS = [
    "mint(uint256)", "mint()", "mint(address,uint256)", "mint(uint256,address)",
    "publicMint(uint256)", "publicMint()", "mintPublic(uint256)", "mintPublic()",
    "mintTo(address,uint256)", "claim(uint256)", "claim()", "freeMint(uint256)",
    "freeMint()", "mint(address)",
]
PROOF_SIGS = [
    "mint(uint256,bytes32[])", "mint(bytes32[],uint256)", "mint(bytes32,bytes)",
    "mint(uint256,bytes)", "mintAllowList(uint256,bytes32[])",
    "whitelistMint(uint256,bytes32[])", "allowlistMint(uint256,bytes32[])",
    "mintWhitelist(uint256,bytes32[])", "claim(uint256,bytes32[])",
]
MINT_SIGS = SIMPLE_SIGS + PROOF_SIGS
MINT_SELECTORS = {"0x" + Web3.keccak(text=s)[:4].hex().replace("0x", "").lower() for s in MINT_SIGS}
# SeaDrop routes mints through its own contract - include those so copy-mint sees OpenSea mints
SEADROP_SIGS = [
    "mintPublic(address,address,address,uint256)",
    "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)",
    "mintAllowList(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])",
]
MINT_SELECTORS |= {"0x" + Web3.keccak(text=s)[:4].hex().replace("0x", "").lower() for s in SEADROP_SIGS}


def selector(sig):
    return Web3.keccak(text=sig)[:4].hex().replace("0x", "").lower()


def sig_types(sig):
    inner = sig[sig.index("(") + 1:sig.rindex(")")]
    return [t for t in inner.split(",") if t]


def get_code(_w3, addr):
    h = _w3.eth.get_code(addr).hex()
    return h if h.startswith("0x") else "0x" + h


# OpenSea SeaDrop (mints route through this contract, not the NFT contract)
SEADROP_ADDR = Web3.to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
OPENSEA_FEE_RECIPIENT = Web3.to_checksum_address("0x0000a26b00c1F0DF003000390027140000fAa719")
SEADROP_FEE_ABI = [{
    "inputs": [{"name": "nftContract", "type": "address"}],
    "name": "getAllowedFeeRecipients",
    "outputs": [{"name": "", "type": "address[]"}],
    "stateMutability": "view", "type": "function",
}]
SEADROP_ABI = [{
    "inputs": [{"name": "nftContract", "type": "address"}],
    "name": "getPublicDrop",
    "outputs": [{"components": [
        {"name": "mintPrice", "type": "uint80"},
        {"name": "startTime", "type": "uint48"},
        {"name": "endTime", "type": "uint48"},
        {"name": "maxTotalMintableByWallet", "type": "uint16"},
        {"name": "feeBps", "type": "uint16"},
        {"name": "restrictFeeRecipients", "type": "bool"},
    ], "name": "", "type": "tuple"}],
    "stateMutability": "view", "type": "function",
}]


def read_public_drop(_w3, collection):
    c = _w3.eth.contract(address=SEADROP_ADDR, abi=SEADROP_ABI)
    return c.functions.getPublicDrop(Web3.to_checksum_address(collection)).call()


def resolve_fee_recipient(_w3, collection):
    """SeaDrop reverts on a disallowed fee recipient - read it from chain, don't guess."""
    try:
        c = _w3.eth.contract(address=SEADROP_ADDR, abi=SEADROP_FEE_ABI)
        allowed = c.functions.getAllowedFeeRecipients(
            Web3.to_checksum_address(collection)).call()
        if allowed:
            return Web3.to_checksum_address(allowed[0])
    except Exception:
        pass
    return OPENSEA_FEE_RECIPIENT


def seadrop_calldata(collection, minter, qty):
    sel = selector("mintPublic(address,address,address,uint256)")
    fee = STATE.get("seadrop_fee") or OPENSEA_FEE_RECIPIENT
    enc = abi_encode(
        ["address", "address", "address", "uint256"],
        [Web3.to_checksum_address(collection), Web3.to_checksum_address(fee),
         Web3.to_checksum_address(minter), int(qty)],
    ).hex()
    return "0x" + sel + enc

# ------------------------------------------------------------------ state
STATE = {
    "wallets":      [],     # list of {name,key,account,address,qty,value_wei,calldata,armed,armed_nonce}
    "contract":     None,
    "abi":          None,
    "fn":           None,   # chosen mint function name
    "fn_inputs":    None,   # list of solidity input types for chosen fn
    "qty":          1,      # default qty per wallet
    "value_wei":    0,      # price per single mint (total = price * qty per wallet)
    "auto_tasks":   {},     # name -> asyncio.Task
    "sched_task":   None,
    "sched_target": None,
    "gas_max_fee":  None,
    "gas_priority": None,
    "gas_limit_override": None,
    "copy_tasks":   {},
    "copy_labels":  {},
    "os_sessions":  {},
    "os_slug":      None,
    "os_ctx":       None,
    "os_qty":       None,
    "awaiting_copy": False,
    "seadrop":      False,
    "seadrop_collection": None,
    "seadrop_fee":  None,
    "pending":      {},
    "pending_seq":  0,
}

_SUBMIT = None


def submit_w3():
    global _SUBMIT
    if _SUBMIT is None:
        _SUBMIT = Web3(Web3.HTTPProvider(SUBMIT_RPC_URL, request_kwargs={"timeout": 10}))
    return _SUBMIT


_POOL_W3 = {}
_EP_COOL = {}          # url -> epoch seconds until it's usable again
RPC_COOLDOWN = float(os.environ.get("RH_RPC_COOLDOWN", "90") or "90")


def w3_for(url):
    if url not in _POOL_W3:
        _POOL_W3[url] = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    return _POOL_W3[url]


def healthy_endpoints():
    now = time.time()
    ok = [u for u in RPC_POOL if _EP_COOL.get(u, 0) <= now]
    return ok or list(RPC_POOL)      # all cooling -> use anyway rather than stall


def mark_rate_limited(url, seconds=None):
    _EP_COOL[url] = time.time() + (seconds if seconds is not None else RPC_COOLDOWN)


def is_rate_limit(err):
    s = str(err).lower()
    return "429" in s or "too many request" in s or "rate limit" in s or "capacity" in s


def pool_w3(i):
    return w3_for(healthy_endpoints()[i % len(healthy_endpoints())])


def wallet_rpc(wal):
    """(web3, url) for this wallet, round-robined over HEALTHY endpoints only."""
    hs = healthy_endpoints()
    url = hs[wal.get("rpc_idx", 0) % len(hs)]
    return w3_for(url), url


def wallet_w3(wal):
    return wallet_rpc(wal)[0]


_RR = {"i": 0}


def w3():
    """General-purpose client. Round-robins over HEALTHY pool endpoints so no single
    key gets hammered (this used to pin every call to RPC_URL)."""
    hs = healthy_endpoints()
    _RR["i"] = (_RR["i"] + 1) % len(hs)
    return w3_for(hs[_RR["i"]])



# ---- speed layer -------------------------------------------------------
# Robinhood's sequencer is where ordering is decided; sending straight to it
# removes a hop. Always included in the blast set.
SEQUENCER_URL = os.environ.get(
    "RH_SEQUENCER_URL", "https://sequencer.mainnet.chain.robinhood.com").strip()


def blast_targets():
    """Every endpoint a raw tx should be fired at, sequencer first."""
    seen, out = set(), []
    for u in ([SEQUENCER_URL] if SEQUENCER_URL else []) + healthy_endpoints() + [SUBMIT_RPC_URL]:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _post_raw(url, body, timeout=10):
    req = urllib.request.Request(
        url, data=body.encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def blast_raw(raw_tx):
    """Fire one signed tx at EVERY endpoint at once; first to reach the sequencer wins.
    Duplicates are harmless - the chain dedupes by tx hash ('already known')."""
    raw_hex = raw_tx.hex() if isinstance(raw_tx, (bytes, bytearray)) else str(raw_tx)
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    txh = "0x" + Web3.keccak(hexstr=raw_hex).hex().replace("0x", "")
    body = json.dumps({"jsonrpc": "2.0", "method": "eth_sendRawTransaction",
                       "params": [raw_hex], "id": 1})
    targets = blast_targets()
    errs = []
    with ThreadPoolExecutor(max_workers=max(2, len(targets))) as ex:
        futs = [ex.submit(_post_raw, u, body) for u in targets]
        for f in futs:
            try:
                j = f.result(timeout=10)
                if j.get("result"):
                    return j["result"]
                if j.get("error"):
                    m = str(j["error"].get("message", j["error"]))
                    if "already known" in m.lower() or "already" in m.lower():
                        return txh  # another endpoint got it there first
                    errs.append(m)
            except Exception as e:
                errs.append(str(e)[:60])
    if errs:
        raise RuntimeError(errs[0][:160])
    return txh


def warm_all():
    """Pre-open TCP/TLS to every endpoint so the fire pays no handshake cost.
    Uses a deliberately invalid sendRawTransaction - the error is fine, the
    established connection is the point (works on send-only sequencers too)."""
    body = json.dumps({"jsonrpc": "2.0", "method": "eth_sendRawTransaction",
                       "params": ["0x00"], "id": 1})
    targets = blast_targets()
    with ThreadPoolExecutor(max_workers=max(2, len(targets))) as ex:
        futs = [ex.submit(_post_raw, u, body, 5) for u in targets]
        for f in futs:
            try:
                f.result(timeout=5)
            except Exception:
                pass
    return len(targets)


async def run_blocking(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# ------------------------------------------------------------------ wallets
def load_wallets():
    out = []
    if os.path.exists(WALLETS_FILE):
        try:
            os.chmod(WALLETS_FILE, 0o600)
        except Exception:
            pass
        raw = json.load(open(WALLETS_FILE))
        for i, w in enumerate(raw):
            key = (w.get("key") or "").strip()
            if not key:
                continue
            out.append({
                "name": w.get("name") or f"w{i+1}",
                "key": key,
                "qty": int(w.get("qty", 1) or 1),
                "value_wei": int(Web3.to_wei(Decimal(str(w.get("value", "0") or "0")), "ether")),
                "calldata": (w.get("calldata") or "").strip(),
            })
    elif PRIVATE_KEY:
        out.append({"name": "w1", "key": PRIVATE_KEY, "qty": 1, "value_wei": 0, "calldata": ""})
    acct_factory = Web3().eth.account
    for i, w in enumerate(out):
        w["account"] = acct_factory.from_key(w["key"])  # raises on a bad key
        w["address"] = w["account"].address
        w["armed"] = None
        w["armed_nonce"] = None
        w["rpc_idx"] = i
    return out


# ------------------------------------------------------------------ tx helpers
def suggest_fees(_w3):
    if STATE.get("gas_max_fee"):
        return STATE["gas_max_fee"], (STATE.get("gas_priority") or _w3.to_wei("0.01", "gwei"))
    try:
        base = _w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:
        base = None
    prio = _w3.to_wei("0.01", "gwei")
    if base is None:
        return _w3.eth.gas_price, prio
    return int(base) * 2 + prio, prio


def total_spend_eth(wallets=None):
    ws = wallets if wallets is not None else STATE["wallets"]
    return sum((Decimal(wallet_value(w)) / Decimal(10 ** 18)) for w in ws)


def spend_guard(wallets=None):
    """Refuse a batch that would spend more than RH_MAX_SPEND_ETH in one go."""
    total = total_spend_eth(wallets)
    if total > MAX_SPEND_ETH:
        raise RuntimeError(
            f"SPEND GUARD: this would spend {total} ETH across wallets, over the "
            f"{MAX_SPEND_ETH} ETH limit. Raise RH_MAX_SPEND_ETH in .env if intended.")
    return total


def wallet_value(wal):
    if wal["value_wei"] > 0:
        return wal["value_wei"]
    return STATE["value_wei"] * (wal["qty"] or STATE["qty"])


def wallet_calldata(_w3, wal):
    if wal["calldata"]:
        return wal["calldata"]
    if STATE.get("seadrop"):
        return seadrop_calldata(STATE["seadrop_collection"], wal["address"], wal["qty"] or STATE["qty"])
    if not STATE["fn"]:
        raise ValueError("no mint function set - run /source, or add per-wallet calldata")
    inputs = STATE["fn_inputs"] or []
    qty = wal["qty"] or STATE["qty"]
    args = []
    for t in inputs:
        if t.startswith("uint"):
            args.append(int(qty))
        elif t == "address":
            args.append(Web3.to_checksum_address(wal["address"]))
        else:
            raise ValueError(f"can't auto-build {STATE['fn']}({','.join(inputs)}) - needs per-wallet calldata")
    sig = f"{STATE['fn']}({','.join(inputs)})"
    sel = selector(sig)
    enc = abi_encode(inputs, args).hex() if inputs else ""
    return "0x" + sel + enc


def build_wallet_tx(_w3, wal, nonce=None):
    acct = wal["account"]
    data = wallet_calldata(_w3, wal)
    value = wallet_value(wal)
    if nonce is None:
        nonce = _w3.eth.get_transaction_count(acct.address, "pending")
    max_fee, prio = suggest_fees(_w3)
    try:
        gas = int(_w3.eth.estimate_gas({
            "from": acct.address, "to": STATE["contract"], "data": data, "value": value
        }) * 1.25)
    except Exception:
        gas = STATE.get("gas_limit_override") or DEFAULT_GAS
    if STATE.get("gas_limit_override"):
        gas = STATE["gas_limit_override"]
    return {
        "chainId": CHAIN_ID, "to": STATE["contract"], "from": acct.address,
        "data": data, "value": value, "nonce": nonce, "gas": gas,
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
    }


def sign_wallet(_w3, wal, tx):
    signed = _w3.eth.account.sign_transaction(tx, wal["key"])
    return getattr(signed, "raw_transaction", None) or signed.rawTransaction


def sim_wallet(_w3, wal):
    _w3.eth.call({
        "from": wal["address"], "to": STATE["contract"],
        "data": wallet_calldata(_w3, wal), "value": wallet_value(wal),
    })


def do_send(_w3, raw):
    # blast to sequencer + all pool endpoints at once (fastest path in)
    try:
        return blast_raw(raw)
    except Exception:
        h = _w3.eth.send_raw_transaction(raw).hex()
        return h if h.startswith("0x") else "0x" + h


# ------------------------------------------------------------------ discovery
def fetch_abi(addr):
    url = f"{BLOCKSCOUT}?module=contract&action=getabi&address={addr}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode())
    if str(data.get("status")) == "1" and data.get("result"):
        return json.loads(data["result"])
    return None


def opensea_slug_to_contract(slug):
    url = f"https://api.opensea.io/api/v2/collections/{slug}"
    req = urllib.request.Request(url, headers={"X-API-KEY": OPENSEA_KEY, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    for c in (data.get("contracts") or []):
        if c.get("address"):
            return c["address"]
    return None


def resolve_source(text):
    """Return (checksum_address_or_None, human_note)."""
    text = text.strip()
    um = re.search(r"opensea\.io/(?:assets|item)/[^/]+/(0x[a-fA-F0-9]{40})", text)
    if um:
        return Web3.to_checksum_address(um.group(1)), "from OpenSea item link"
    cm = re.search(r"opensea\.io/collection/([A-Za-z0-9\-_]+)", text)
    if cm:
        slug = cm.group(1)
        if OPENSEA_KEY:
            try:
                addr = opensea_slug_to_contract(slug)
                if addr:
                    return Web3.to_checksum_address(addr), f"from OpenSea collection '{slug}'"
                return None, f"OpenSea returned no contract for '{slug}'."
            except Exception as e:
                return None, f"OpenSea API error for '{slug}': {e}"
        return None, ("That's a collection link. Either set OPENSEA_API_KEY, or open the collection "
                      "on OpenSea -> Details -> copy the Contract Address and send that instead "
                      "(a raw 0x address is the most reliable input).")
    m = re.search(r"0x[a-fA-F0-9]{40}", text)
    if m:
        return Web3.to_checksum_address(m.group(0)), "contract address"
    return None, "Couldn't find a contract address or a recognizable OpenSea item link."


def detect_mint_fns(abi):
    out = []
    for i in abi:
        if i.get("type") != "function":
            continue
        if i.get("stateMutability") not in ("payable", "nonpayable"):
            continue
        low = i.get("name", "").lower()
        if "mint" in low or "claim" in low:
            out.append((i["name"], [a["type"] for a in i.get("inputs", [])]))

    def score(t):
        nm, inp = t
        s = 0
        if nm.lower() == "mint":
            s -= 10
        if len(inp) in (0, 1):
            s -= 3
        if inp[:1] == ["uint256"]:
            s -= 1
        return s

    out.sort(key=score)
    return out


def read_price(_w3, addr, abi):
    getters = ("mintPrice", "price", "cost", "PRICE", "publicPrice", "getPrice", "itemPrice", "tokenPrice")
    names = {i.get("name") for i in abi if i.get("type") == "function"}
    c = _w3.eth.contract(address=addr, abi=abi)
    for g in getters:
        if g in names:
            try:
                v = c.functions[g]().call()
                if isinstance(v, int) and v >= 0:
                    return g, v
            except Exception:
                pass
    return None, None


def parse_when(s):
    s = s.strip()
    if s.startswith("+"):
        parts = re.findall(r"(\d+)([dhms])", s[1:].lower())
        if not parts:
            raise ValueError("bad duration - try +90m or +2h30m or +45s")
        mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        return time.time() + sum(int(n) * mult[u] for n, u in parts)
    if re.fullmatch(r"\d{10}", s):
        return float(s)
    if re.fullmatch(r"\d{13}", s):
        return float(s) / 1000.0
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("add a timezone - append Z for UTC or an offset like +06:00, or use epoch")
    return dt.timestamp()


def _fmt_eta(target):
    d = int(target - time.time())
    if d <= 0:
        return "now"
    h, r = divmod(d, 3600)
    m, s = divmod(r, 60)
    out = []
    if h:
        out.append(f"{h}h")
    if m:
        out.append(f"{m}m")
    out.append(f"{s}s")
    return " ".join(out)



# =====================================================================
# OpenSea private API: SIWE auth -> eligibility -> mint calldata
# ---------------------------------------------------------------------
# This is how WL / FCFS ("signed presale") mints get automated. OpenSea's
# backend issues a per-wallet signature; you get it by authenticating the
# wallet with Sign-In-With-Ethereum and asking their GraphQL for the mint
# action. It returns the COMPLETE calldata (signature included) which we
# then validate locally and fire ourselves.
# =====================================================================
OS_SITE = "https://opensea.io"
OS_GQL = "https://gql.opensea.io/graphql"
OS_APP_ID = "os2-web"
# OpenSea throttles bursts from one IP - keep concurrency low and space requests out.
OS_CONCURRENCY = int(os.environ.get("OS_CONCURRENCY", "2") or "2")
OS_DELAY = float(os.environ.get("OS_DELAY", "1.2") or "1.2")
OS_RETRIES = int(os.environ.get("OS_RETRIES", "6") or "6")
_OS_SEM = None


def os_sem():
    global _OS_SEM
    if _OS_SEM is None:
        _OS_SEM = asyncio.Semaphore(max(1, OS_CONCURRENCY))
    return _OS_SEM


def is_os_rate_limited(err):
    s = str(err).lower()
    return "429" in s or "too many request" in s
OS_SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)
STAGE_SELECTOR = {
    "PUBLIC_SALE": "0x161ac21f",
    "SIGNED_PRESALE": "0x4b61cd6f",
    "MERKLE_PRESALE": "0x4300a4e6",
}

OS_COLLECTION_QUERY = """
query MintCollectionMetadata($slug: String!) {
  collectionBySlug(slug: $slug) {
    __typename
    ... on Collection {
      slug address
      chain { identifier networkId }
      drop {
        __typename
        identifier { contractAddress chain { identifier } }
        stages {
          __typename stageType stageIndex startTime endTime
          maxTotalMintableByWallet
        }
      }
    }
  }
}
"""

OS_ELIGIBILITY_QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 { minterQuantityMinted(minter: $address) }
    stages {
      __typename stageType stageIndex isEligible eligibleMinterAddress
      maxTotalMintableByWallet eligibleMaxTotalMintableByWallet
      eligiblePrice { token { unit symbol contractAddress chain { identifier } } }
    }
  }
}
"""

OS_MINT_QUERY = """
query MintActionTimelineQuery($address: Address!, $fromAssets: [AssetQuantityInput!]!,
                              $toAssets: [AssetQuantityInput!]!, $recipient: Address) {
  swap(address: $address, fromAssets: $fromAssets, toAssets: $toAssets,
       recipient: $recipient, action: MINT) {
    actions {
      __typename
      ... on TransactionAction {
        transactionSubmissionData { to data value chain { networkId identifier } }
      }
    }
    errors { __typename }
  }
}
"""


async def os_retry(fn, *args, label=""):
    """Run an OpenSea call under the concurrency limit, backing off on 429."""
    delay = 1.5
    last = None
    for attempt in range(OS_RETRIES):
        async with os_sem():
            try:
                res = await run_blocking(fn, *args)
                await asyncio.sleep(OS_DELAY)      # space out the next one
                return res
            except Exception as e:
                last = e
                if not is_os_rate_limited(e):
                    raise
        await asyncio.sleep(delay)                 # released the slot while waiting
        delay = min(delay * 2, 45.0)
    raise last if last else RuntimeError("OpenSea retry failed")


def os_slug(text):
    """Accept a slug, an opensea.io/collection/<slug> URL, or return None."""
    text = (text or "").strip()
    m = re.search(r"opensea\.io/collection/([A-Za-z0-9\-_]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_]{1,200}", text) and not text.startswith("0x"):
        return text
    return None


def os_session():
    import requests
    s = requests.Session()
    s.headers.update({
        "accept": "application/json",
        "origin": OS_SITE,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    })
    return s


def os_login(wal, slug, chain_id):
    """SIWE-authenticate one wallet against OpenSea. Returns a live session."""
    from eth_account.messages import encode_defunct
    from datetime import datetime, timezone as _tz
    s = os_session()
    ref = f"{OS_SITE}/collection/{slug}/overview"
    addr = Web3.to_checksum_address(wal["address"])
    s.cookies.set("connected-account-server-hint", addr.lower(), domain="opensea.io", path="/")
    r = s.post(f"{OS_SITE}/__api/auth/siwe/nonce", headers={"referer": ref}, timeout=15)
    r.raise_for_status()
    nonce = r.json().get("nonce")
    if not nonce:
        raise RuntimeError("no nonce from OpenSea")
    issued = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(_tz.utc).microsecond // 1000:03d}Z"
    msg = (f"opensea.io wants you to sign in with your Ethereum account:\n{addr}\n\n"
           f"{OS_SIWE_STATEMENT}\n\nURI: {ref}\nVersion: 1\nChain ID: {chain_id}\n"
           f"Nonce: {nonce}\nIssued At: {issued}")
    signed = Web3().eth.account.sign_message(encode_defunct(text=msg), private_key=wal["key"])
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    body = {
        "message": {
            "domain": "opensea.io", "address": addr, "statement": OS_SIWE_STATEMENT,
            "uri": ref, "version": "1", "chainId": str(chain_id), "nonce": nonce,
            "issuedAt": issued, "accountType": "Ethereum",
        },
        "signature": sig, "chainArch": "EVM",
    }
    r = s.post(f"{OS_SITE}/__api/auth/siwe/verify", json=body,
               headers={"referer": ref, "content-type": "application/json"}, timeout=20)
    if not r.ok:
        raise RuntimeError(f"SIWE verify HTTP {r.status_code}")
    got = (r.json().get("user") or {}).get("address", "")
    if got.lower() != addr.lower():
        raise RuntimeError("session wallet mismatch")
    return s


def os_graphql(session, slug, op, query, variables):
    ref = f"{OS_SITE}/collection/{slug}/overview"
    r = session.post(OS_GQL, json={"operationName": op, "query": query, "variables": variables},
                     headers={"referer": ref, "x-app-id": OS_APP_ID,
                              "content-type": "application/json"}, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("OpenSea rate limited")
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(str(j["errors"])[:120])
    if not j.get("data"):
        raise RuntimeError("empty OpenSea response")
    return j["data"]


def os_collection(session, slug):
    d = os_graphql(session, slug, "MintCollectionMetadata", OS_COLLECTION_QUERY, {"slug": slug})
    c = d.get("collectionBySlug")
    if not c:
        raise RuntimeError("collection not found")
    drop = c.get("drop") or {}
    return {
        "slug": c.get("slug"),
        "address": Web3.to_checksum_address(c["address"]),
        "chain": (c.get("chain") or {}).get("identifier"),
        "network_id": int((c.get("chain") or {}).get("networkId") or 0),
        "drop_kind": drop.get("__typename"),
        "drop_address": Web3.to_checksum_address((drop.get("identifier") or {})["contractAddress"]),
        "stages": drop.get("stages") or [],
    }


def os_eligibility(session, slug, wal):
    d = os_graphql(session, slug, "DropEligibilityQuery", OS_ELIGIBILITY_QUERY,
                   {"collectionSlug": slug, "address": wal["address"].lower()})
    drop = d.get("dropBySlug") or {}
    out = []
    for st in drop.get("stages") or []:
        price = None
        ep = st.get("eligiblePrice")
        if ep and ep.get("token") is not None:
            try:
                price = int(Web3.to_wei(Decimal(str(ep["token"]["unit"])), "ether"))
            except Exception:
                price = None
        out.append({
            "type": st.get("stageType"),
            "index": int(st.get("stageIndex") or 0),
            "eligible": bool(st.get("isEligible")),
            "max": st.get("eligibleMaxTotalMintableByWallet") or st.get("maxTotalMintableByWallet"),
            "price_wei": price,
        })
    return out, drop.get("minterQuantityMinted")


def os_mint_action(session, coll, wal, qty, token_id="0"):
    """Ask OpenSea to build the mint tx for THIS wallet. For a signed presale the
    returned calldata already contains that wallet's server signature."""
    native = "0x0000000000000000000000000000000000000000"
    variables = {
        "address": Web3.to_checksum_address(wal["address"]),
        "fromAssets": [{"asset": {"contractAddress": native, "chain": coll["chain"]}}],
        "toAssets": [{"asset": {"contractAddress": coll["drop_address"], "chain": coll["chain"],
                                "tokenId": token_id}, "quantity": str(qty)}],
        "recipient": None,
    }
    d = os_graphql(session, coll["slug"], "MintActionTimelineQuery", OS_MINT_QUERY, variables)
    swap = d.get("swap") or {}
    errs = [e.get("__typename") for e in (swap.get("errors") or [])]
    if errs:
        raise RuntimeError(errs[0])
    for a in swap.get("actions") or []:
        tsd = a.get("transactionSubmissionData")
        if tsd:
            return {
                "to": Web3.to_checksum_address(tsd["to"]),
                "data": tsd["data"],
                "value": int(tsd.get("value") or 0),
                "network_id": int((tsd.get("chain") or {}).get("networkId") or 0),
            }
    raise RuntimeError("no transaction action returned")


def os_validate(action, coll, wal, stage_type, qty, chain_id):
    """Never fire whatever OpenSea hands back without checking it ourselves."""
    data = action["data"]
    if not data.startswith("0x") or len(data) < 10:
        raise RuntimeError("bad calldata")
    if action["network_id"] and action["network_id"] != chain_id:
        raise RuntimeError(f"wrong chain {action['network_id']}")
    want = STAGE_SELECTOR.get(stage_type)
    if want and data[:10].lower() != want:
        raise RuntimeError(f"selector {data[:10]} != {stage_type}")
    # word-level checks below assume the ERC721 SeaDrop layout; skip for others
    if coll.get("drop_kind") not in (None, "Erc721SeaDropV1"):
        return True
    body = data[10:]

    def word(i):
        return body[i * 64:(i + 1) * 64]

    nft = Web3.to_checksum_address("0x" + word(0)[24:])
    if nft.lower() != coll["address"].lower():
        raise RuntimeError("nft contract mismatch")
    minter = "0x" + word(2)[24:]
    if int(minter, 16) != 0 and minter.lower() != wal["address"].lower():
        raise RuntimeError("minter mismatch")
    if int(word(3), 16) != int(qty):
        raise RuntimeError("quantity mismatch")
    return True


# ------------------------------------------------------------------ auth
def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if (not OWNER_ID) or uid != OWNER_ID:   # fail closed
            if update.message:
                await update.effective_message.reply_text("Unauthorized.")
            log.warning("Rejected command from user %s", uid)
            return
        return await fn(update, context)
    return wrapper


# ------------------------------------------------------------------ commands
@owner_only
async def start_cmd(update, context):
    await update.effective_message.reply_text(
        "Robinhood Chain MULTI-WALLET mint bot - self-hosted.\n\n"
        "Keys live in wallets.json on this machine only, never in chat. Only your Telegram ID "
        "can command this bot. FCFS chain: pre-signed + closest-to-sequencer wins, not gas.\n\n"
        "It auto-builds calldata for simple/public mints. Proof-gated whitelists need each wallet's "
        "own calldata from the project (the bot flags this in /source).\n\n"
        "Type /help."
    )


@owner_only
async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "SETUP\n"
        "/wallets - list your wallets + balances\n"
        "/source <opensea link | 0xcontract> - resolve contract, ABI, mint fn, price\n"
        "/setfn <fn> - override the detected mint function\n"
        "/setqty <n> - default quantity per wallet\n"
        "/setvalue 0.05 - price per mint (if payable)\n"
        "/status - full config\n\n"
        "CHECK + FIRE (all wallets)\n"
        "/simall - simulate each wallet (eligibility, spends nothing)\n"
        "/armall - pre-sign a tx for every wallet\n"
        "/mintall - fire from every eligible wallet now\n"
        "/autoall [ms] - each wallet fires the instant IT becomes eligible\n"
        "/scheduleall <when> [leadSec] [pollMs] - auto-mint all at WL start\n"
        "/cancel - stop everything\n\n"
        "WALLETS & GAS\n"
        "/newwallet [n] - create n new wallets\n"
        "/importwallet 0xKEY - add an existing wallet\n"
        "/setgas <maxFeeGwei> [prioGwei] [gasLimit] - gas override (auto resets)\n"
        "/copy 0xADDR [sec] - copy-mint a target wallets mints\n"
        "/cancelauto - stop auto-mint only\n"
        "/cancelschedule - stop scheduled mint only\n"
        "/cancelcopy - stop copy-mint only\n"
        "/cancel - stop EVERYTHING\n"
        "/oscheck <collection> - OpenSea WL/FCFS/public eligibility per wallet\n"
        "/osmint <STAGE> [qty] - mint a WL/FCFS/public phase via OpenSea\n"
        "/oslogout - clear OpenSea sessions (if they go stale)\n"
        "/osqty <n> - per-wallet qty for OpenSea mints (default: stage max)\n"
        "/proofapi <url> - fetch merkle proofs for all wallets from a site API\n"
        "/setproof <wallet> 0x.. - set one wallet calldata manually\n"
        "/mintloop <n> [delayMs] - send n mint txs per wallet (high-cap drops)\n"
        "/copy 0xADDR [name] - track a wallet (named alerts)\n"
        "/rename 0xADDR <name> - rename a tracked wallet\n"
        "/copywatch - view/manage copy watchlist (buttons)\n"
        "/rpcstatus - check the RPC pool endpoints\n"
        "(tip: just paste a contract/OS link - no /source needed)"
    )


@owner_only
async def wallets_cmd(update, context):
    lines = [f"{len(STATE['wallets'])} wallet(s):"]
    for wal in STATE["wallets"]:
        tag = "proof-set" if wal["calldata"] else "auto"
        _w3, _url = wallet_rpc(wal)      # spread balance checks across the pool
        try:
            bal = _w3.eth.get_balance(wal["address"])
            lines.append(f"[{wal['name']}] {wal['address'][:8]}..{wal['address'][-4:]}  "
                         f"{Decimal(bal)/Decimal(10**18):.5f} ETH  qty {wal['qty']}  {tag}")
        except Exception as e:
            if is_rate_limit(e):
                mark_rate_limited(_url)
            lines.append(f"[{wal['name']}] {wal['address'][:8]}..  RPC err  {tag}")
    await update.effective_message.reply_text("\n".join(lines))


def action_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Simulate", callback_data="act|sim"),
         InlineKeyboardButton("Arm", callback_data="act|arm")],
        [InlineKeyboardButton("MINT ALL NOW", callback_data="act|mint"),
         InlineKeyboardButton("Auto-mint", callback_data="act|auto")],
    ])


def read_name(_w3, addr):
    try:
        c = _w3.eth.contract(
            address=Web3.to_checksum_address(addr),
            abi=[{"inputs": [], "name": "name", "outputs": [{"type": "string"}],
                  "stateMutability": "view", "type": "function"}])
        return c.functions.name().call()
    except Exception:
        return None


async def _try_seadrop(_w3, addr, msg):
    try:
        pd = await run_blocking(read_public_drop, _w3, addr)
    except Exception:
        pd = None
    if pd and (int(pd[3]) > 0 or int(pd[2]) > 0):
        STATE["seadrop"] = True
        STATE["seadrop_collection"] = addr
        STATE["contract"] = SEADROP_ADDR
        STATE["value_wei"] = int(pd[0])
        try:
            STATE["seadrop_fee"] = await run_blocking(resolve_fee_recipient, _w3, addr)
        except Exception:
            STATE["seadrop_fee"] = OPENSEA_FEE_RECIPIENT
        msg.append(f"OpenSea SeaDrop mint - price {Decimal(int(pd[0]))/Decimal(10**18)} ETH, "
                   f"max {int(pd[3])}/wallet. Calldata auto-built for every wallet.")
        return True
    return False


async def do_source(update, context, text):
    reply = update.effective_message.reply_text
    addr, note = resolve_source(text)
    if not addr:
        await reply(note)
        return
    STATE.update(contract=addr, abi=None, fn=None, fn_inputs=None, seadrop=False, seadrop_collection=None)
    for wal in STATE["wallets"]:
        wal["armed"] = None
    _w3 = w3()
    name = await run_blocking(read_name, _w3, addr)
    msg = [f"Collection: {name}" if name else "Collection: (unnamed)", f"Contract: {addr}"]
    mintable = False
    try:
        abi = await run_blocking(fetch_abi, addr)
    except Exception:
        abi = None
    if abi:
        STATE["abi"] = abi
        fns = detect_mint_fns(abi)
        if fns:
            try:
                pg, pv = await run_blocking(read_price, _w3, addr, abi)
            except Exception:
                pg, pv = None, None
            if pv is not None:
                STATE["value_wei"] = pv
                msg.append(f"Price: {Decimal(pv)/Decimal(10**18)} ETH")
            top_nm, top_inp = fns[0]
            simple = (len(top_inp) == 0
                      or (len(top_inp) == 1 and top_inp[0].startswith("uint"))
                      or (len(top_inp) == 2 and top_inp[0] == "address" and top_inp[1].startswith("uint")))
            if simple:
                STATE["fn"], STATE["fn_inputs"] = top_nm, top_inp
                msg.append(f"SIMPLE mint {top_nm}({','.join(top_inp)}) - calldata auto-built for all wallets.")
                mintable = True
            else:
                msg.append(f"WHITELIST mint {top_nm}({','.join(top_inp)}) - needs each wallet's calldata "
                           "(proof issued by the project).")
        else:
            mintable = await _try_seadrop(_w3, addr, msg)
            if not mintable:
                msg.append("No mint function in the ABI. Capture calldata from a real mint tx -> wallets.json.")
    else:
        try:
            code = await run_blocking(get_code, _w3, addr)
        except Exception:
            code = "0x"
        hexcode = code[2:].lower() if code else ""
        if not hexcode:
            msg.append("No contract code at that address - double-check it.")
        else:
            found_simple = [s for s in SIMPLE_SIGS if ("63" + selector(s)) in hexcode]
            found_proof = [s for s in PROOF_SIGS if ("63" + selector(s)) in hexcode]
            if found_simple:
                sig = found_simple[0]
                STATE["fn"], STATE["fn_inputs"] = sig.split("(")[0], sig_types(sig)
                msg.append(f"Unverified; bytecode shows {sig} - SIMPLE mint, calldata auto-built.")
                mintable = True
            elif found_proof:
                msg.append(f"Unverified; bytecode shows {found_proof[0]} - PROOF/whitelist. "
                           "Needs per-wallet calldata.")
            else:
                mintable = await _try_seadrop(_w3, addr, msg)
                if not mintable:
                    msg.append("No known mint selector. Capture calldata from a real mint tx -> wallets.json.")
    if mintable:
        msg.append(f"{len(STATE['wallets'])} wallets | qty {STATE['qty']}. Tap to act:")
        await reply("\n".join(msg), reply_markup=action_kb())
    else:
        await reply("\n".join(msg))


@owner_only
async def source_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /source <opensea link or 0xcontract> - or just paste the address, no command needed.")
        return
    _txt = " ".join(context.args)
    if "opensea.io/collection/" in _txt:
        _s = os_slug(_txt)
        if _s:
            await os_autoload(update, context, _s)
            return
    await do_source(update, context, _txt)


@owner_only
async def setfn_cmd(update, context):
    if not STATE["abi"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    if not context.args:
        fns = detect_mint_fns(STATE["abi"])
        await update.effective_message.reply_text("Pick: " + ", ".join(f"{n}({','.join(inp)})" for n, inp in fns))
        return
    name = context.args[0]
    for i in STATE["abi"]:
        if i.get("type") == "function" and i.get("name") == name:
            STATE["fn"] = name
            STATE["fn_inputs"] = [a["type"] for a in i.get("inputs", [])]
            for wal in STATE["wallets"]:
                wal["armed"] = None
            await update.effective_message.reply_text(f"Mint fn set: {name}({','.join(STATE['fn_inputs'])})")
            return
    await update.effective_message.reply_text("No such function in the ABI.")


@owner_only
async def setqty_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /setqty 2")
        return
    try:
        STATE["qty"] = max(1, int(context.args[0]))
    except ValueError:
        await update.effective_message.reply_text("bad number")
        return
    for wal in STATE["wallets"]:
        wal["armed"] = None
    await update.effective_message.reply_text(f"Default qty per wallet: {STATE['qty']} (per-wallet qty overrides).")


@owner_only
async def setvalue_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /setvalue 0.05  (price per single mint)")
        return
    try:
        STATE["value_wei"] = int(Web3.to_wei(Decimal(context.args[0]), "ether"))
    except Exception:
        await update.effective_message.reply_text("bad amount")
        return
    for wal in STATE["wallets"]:
        wal["armed"] = None
    await update.effective_message.reply_text(f"Price/mint: {context.args[0]} ETH (total per wallet = price x qty).")


@owner_only
async def status_cmd(update, context):
    _w3 = w3()
    lines = []
    try:
        lines.append(f"RPC: connected | chainId {_w3.eth.chain_id} | block {_w3.eth.block_number}")
    except Exception as e:
        lines.append(f"RPC ERROR {e}")
    lines.append(f"Submit: {SUBMIT_RPC_URL}")
    lines.append(f"Wallets: {len(STATE['wallets'])}")
    lines.append(f"Contract: {STATE['contract'] or '-'}")
    if STATE.get("seadrop"):
        lines.append(f"SeaDrop: {STATE['seadrop_collection']}")
    if STATE["fn"]:
        lines.append(f"Mint fn: {STATE['fn']}({','.join(STATE['fn_inputs'] or [])})")
    else:
        lines.append("Mint fn: - (proof mint -> per-wallet calldata, or run /source)")
    lines.append(f"Default qty: {STATE['qty']}  price/mint: {Decimal(STATE['value_wei'])/Decimal(10**18)} ETH")
    armed = sum(1 for w in STATE["wallets"] if w.get("armed"))
    lines.append(f"Armed: {armed}/{len(STATE['wallets'])}")
    running = any(t and not t.done() for t in STATE["auto_tasks"].values())
    lines.append(f"Auto: {'running' if running else 'off'}")
    if STATE["sched_target"] and STATE["sched_task"] and not STATE["sched_task"].done():
        utc = datetime.fromtimestamp(STATE["sched_target"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"Scheduled: {utc} (in {_fmt_eta(STATE['sched_target'])})")
    else:
        lines.append("Scheduled: no")
    if STATE.get("gas_max_fee"):
        lines.append("Gas override: maxFee " + str(Decimal(STATE["gas_max_fee"]) / Decimal(10**9)) + " gwei")
    _active = sum(1 for t in STATE["copy_tasks"].values() if t and not t.done())
    lines.append(f"Copy-mint: {_active} wallet(s) tracked" if _active else "Copy-mint: off")
    await update.effective_message.reply_text("\n".join(lines))


async def _confirm(update, _w3, txh, name):
    try:
        rcpt = await run_blocking(_w3.eth.wait_for_transaction_receipt, txh, 60)
        ok = rcpt.get("status") == 1
        await update.effective_message.reply_text(
            f"[{name}] " + ("MINED - success" if ok else "MINED but reverted") + f" | block {rcpt.get('blockNumber')}"
        )
    except Exception:
        await update.effective_message.reply_text(f"[{name}] not confirmed in 60s - check the explorer.")


@owner_only
async def simall_cmd(update, context):
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    _w3 = w3()

    async def one(wal):
        _w3 = wallet_w3(wal)
        try:
            await run_blocking(sim_wallet, _w3, wal)
            return f"[{wal['name']}] ELIGIBLE - would mint now"
        except Exception as e:
            return f"[{wal['name']}] not yet: {safe(e, 70)}"

    res = await asyncio.gather(*[one(w) for w in STATE["wallets"]])
    await update.effective_message.reply_text("\n".join(["Eligibility (simulation, spends nothing):"] + list(res)))


@owner_only
async def armall_cmd(update, context):
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    _w3 = w3()

    async def one(wal):
        _w3 = wallet_w3(wal)
        try:
            tx = await run_blocking(build_wallet_tx, _w3, wal, None)
            wal["armed"] = sign_wallet(_w3, wal, tx)
            wal["armed_nonce"] = tx["nonce"]
            return f"[{wal['name']}] armed nonce {tx['nonce']}"
        except Exception as e:
            wal["armed"] = None
            return f"[{wal['name']}] arm failed: {safe(e, 70)}"

    res = await asyncio.gather(*[one(w) for w in STATE["wallets"]])
    try:
        n_warm = await run_blocking(warm_all)
    except Exception:
        n_warm = 0
    await update.effective_message.reply_text(
        "\n".join([f"Armed ({n_warm} endpoints warmed, blast-send enabled):"] + list(res)))


async def _fire_all(update, only_eligible=True):
    _w3 = w3(); _sw = submit_w3()

    async def one(wal):
        _w3 = wallet_w3(wal)
        _sw = _w3
        try:
            if only_eligible:
                try:
                    await run_blocking(sim_wallet, _w3, wal)
                except Exception:
                    return (wal["name"], "skip (not eligible/open)")
            raw = wal.get("armed")
            if raw is None:
                tx = await run_blocking(build_wallet_tx, _w3, wal, None)
                raw = sign_wallet(_w3, wal, tx)
            txh = await run_blocking(do_send, _sw, raw)
            wal["armed"] = None
            asyncio.create_task(_confirm(update, _w3, txh, wal["name"]))
            return (wal["name"], f"SENT {txh}")
        except Exception as e:
            return (wal["name"], f"FAIL {safe(e, 80)}")

    return await asyncio.gather(*[one(w) for w in STATE["wallets"]])


@owner_only
async def mintall_cmd(update, context):
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    try:
        total = spend_guard()
    except Exception as e:
        await update.effective_message.reply_text(str(e))
        return
    if total > 0:
        await update.effective_message.reply_text(f"Spending ~{total} ETH across wallets...")
    res = await _fire_all(update, only_eligible=True)
    await update.effective_message.reply_text("\n".join(["Mint all:"] + [f"[{n}] {r}" for n, r in res]))


def wallets_share_gate():
    """True when one probe answers 'is it open?' for every wallet.

    Two cases qualify:
      1. Identical calldata (plain mint(qty) style) - obviously shared.
      2. SeaDrop, where calldata differs ONLY by the minter address baked in.
         The stage open/closed state is still identical for all wallets, so one
         probe is enough - and this is the common case, so it matters most.
    Per-wallet whitelist calldata (proofs/signatures) does NOT qualify: those
    wallets genuinely have different eligibility and must be probed separately.
    """
    ws = STATE["wallets"]
    if len(ws) < 2:
        return False
    if any(w.get("calldata") for w in ws):
        return False                      # custom per-wallet calldata -> not shared
    try:
        vals = {wallet_value(w) for w in ws}
        if len(vals) != 1:
            return False
        if STATE.get("seadrop"):
            return True                   # only the minter address differs
        _w3 = w3()
        first = wallet_calldata(_w3, ws[0])
        return all(wallet_calldata(_w3, w) == first for w in ws[1:])
    except Exception:
        return False


def adaptive(interval, elapsed):
    """Poll gently while waiting, tighten up once we're clearly in the window.
    Saves enormous quota on long waits without losing reaction speed."""
    if elapsed < 60:
        return max(interval, 1.0)          # first minute: 1s is plenty
    if elapsed < 600:
        return max(interval, 2.0)          # long wait: 2s
    return max(interval, 5.0)              # very long wait: 5s


async def _shared_gate_hunt(update, interval, deadline=None):
    """ONE probe for all wallets, then fire everything the instant it opens."""
    ws = STATE["wallets"]
    canary = ws[0]
    _w3, _url = wallet_rpc(canary)
    started = time.time()
    polls = 0
    await update.effective_message.reply_text(
        f"Watching ONE shared gate for all {len(ws)} wallets (same call) - "
        f"~{len(ws)}x fewer RPC calls. Fires everything the moment it opens.")
    while True:
        if deadline and time.time() > deadline:
            await update.effective_message.reply_text("Window closed - mint never opened.")
            return
        try:
            await run_blocking(sim_wallet, _w3, canary)
        except Exception as e:
            if is_rate_limit(e):
                mark_rate_limited(_url)
                _w3, _url = wallet_rpc(canary)      # rotate to a healthy endpoint
            polls += 1
            await asyncio.sleep(adaptive(interval, time.time() - started))
            continue
        await update.effective_message.reply_text(
            f"MINT OPEN after {polls} probes - firing {len(ws)} wallets")
        try:
            spend_guard()
        except Exception as e:
            await update.effective_message.reply_text(str(e))
            return
        res = await _fire_all(update, only_eligible=False)
        await update.effective_message.reply_text(
            "\n".join(["Fired:"] + [f"[{n}] {r}" for n, r in res]))
        return


async def _wallet_hunt(update, wal, interval, deadline=None):
    _w3, _url = wallet_rpc(wal)
    _sw = _w3
    started = time.time()
    while True:
        if deadline and time.time() > deadline:
            await update.effective_message.reply_text(f"[{wal['name']}] window closed - not eligible/open.")
            return
        try:
            await run_blocking(sim_wallet, _w3, wal)  # no revert -> this wallet can mint now
        except Exception as e:
            if is_rate_limit(e):
                mark_rate_limited(_url)
                _w3, _url = wallet_rpc(wal)
                _sw = _w3
            await asyncio.sleep(adaptive(interval, time.time() - started))
            continue
        try:
            raw = wal.get("armed")
            if raw is None:
                tx = await run_blocking(build_wallet_tx, _w3, wal, None)
                raw = sign_wallet(_w3, wal, tx)
            txh = await run_blocking(do_send, _sw, raw)
            wal["armed"] = None
            await update.effective_message.reply_text(f"[{wal['name']}] SENT {txh}\n{EXPLORER}/tx/{txh}")
            asyncio.create_task(_confirm(update, _w3, txh, wal["name"]))
        except Exception as e:
            await update.effective_message.reply_text(f"[{wal['name']}] fire failed: {safe(e, 120)}")
        return


@owner_only
async def autoall_cmd(update, context):
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    if any(t and not t.done() for t in STATE["auto_tasks"].values()):
        await update.effective_message.reply_text("Auto already running. /cancel first.")
        return
    interval = 0.1
    if context.args:
        try:
            interval = max(0.05, float(context.args[0]) / 1000.0)
        except ValueError:
            pass
    shared = await run_blocking(wallets_share_gate)
    if shared:
        STATE["auto_tasks"] = {
            "_gate": asyncio.create_task(_shared_gate_hunt(update, interval, None))}
    else:
        STATE["auto_tasks"] = {
            wal["name"]: asyncio.create_task(_wallet_hunt(update, wal, interval, None))
            for wal in STATE["wallets"]
        }
        await update.effective_message.reply_text(
            f"Auto-mint watching {len(STATE['wallets'])} wallets separately (different calls). "
            f"Poll {int(interval*1000)}ms, auto-eased while waiting. /cancelauto to stop.")


async def _scheduled_all(update, target, lead, interval, window):
    while True:
        rem = target - time.time() - lead
        if rem <= 0:
            break
        await asyncio.sleep(min(rem, 30))
    _w3 = w3()

    async def arm(wal):
        _w3 = wallet_w3(wal)
        try:
            tx = await run_blocking(build_wallet_tx, _w3, wal, None)
            wal["armed"] = sign_wallet(_w3, wal, tx)
            wal["armed_nonce"] = tx["nonce"]
        except Exception:
            wal["armed"] = None

    await asyncio.gather(*[arm(w) for w in STATE["wallets"]])
    try:
        await run_blocking(lambda: submit_w3().eth.block_number)
    except Exception:
        pass
    await update.effective_message.reply_text("Go-time. Re-armed all wallets. Watching per-wallet eligibility now...")
    STATE["auto_tasks"] = {}
    if await run_blocking(wallets_share_gate):
        t = asyncio.create_task(_shared_gate_hunt(update, interval, target + window))
        STATE["auto_tasks"]["_gate"] = t
        await asyncio.gather(t, return_exceptions=True)
    else:
        tasks = []
        for wal in STATE["wallets"]:
            t = asyncio.create_task(_wallet_hunt(update, wal, interval, target + window))
            STATE["auto_tasks"][wal["name"]] = t
            tasks.append(t)
        await asyncio.gather(*tasks, return_exceptions=True)
    STATE["sched_task"] = None
    STATE["sched_target"] = None


@owner_only
async def scheduleall_cmd(update, context):
    if STATE["sched_task"] and not STATE["sched_task"].done():
        await update.effective_message.reply_text("Already scheduled. /cancel first.")
        return
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /scheduleall <when> [leadSeconds] [pollMs]\n"
            "when = +90m | 2026-07-20T14:00:00Z | 2026-07-20T20:00:00+06:00 | epoch\n"
            "e.g. /scheduleall 2026-07-20T14:00:00Z 3 150"
        )
        return
    try:
        target = parse_when(context.args[0])
    except Exception as e:
        await update.effective_message.reply_text(f"time error: {e}")
        return
    if target <= time.time():
        await update.effective_message.reply_text("That time is in the past.")
        return
    lead, interval, window = 3.0, 0.1, 180.0
    if len(context.args) >= 2:
        try:
            lead = max(0.0, float(context.args[1]))
        except ValueError:
            pass
    if len(context.args) >= 3:
        try:
            interval = max(0.05, float(context.args[2]) / 1000.0)
        except ValueError:
            pass
    STATE["sched_target"] = target
    STATE["sched_task"] = asyncio.create_task(_scheduled_all(update, target, lead, interval, window))
    utc = datetime.fromtimestamp(target, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await update.effective_message.reply_text(
        f"Scheduled all {len(STATE['wallets'])} wallets.\n"
        f"Target: {utc}  (in {_fmt_eta(target)})\n"
        f"Re-arms {int(lead)}s early, then each wallet fires on its own eligibility.\n"
        "Keep this running (a VPS is safer than a laptop for an unattended mint). /cancel to clear."
    )


def _stop_auto():
    n = sum(1 for t in STATE["auto_tasks"].values() if t and not t.done())
    for t in STATE["auto_tasks"].values():
        if t and not t.done():
            t.cancel()
    STATE["auto_tasks"] = {}
    for wal in STATE["wallets"]:
        wal["armed"] = None
        wal["armed_nonce"] = None
    return n


def _stop_schedule():
    had = bool(STATE["sched_task"] and not STATE["sched_task"].done())
    if had:
        STATE["sched_task"].cancel()
    STATE["sched_task"] = None
    STATE["sched_target"] = None
    return had


def _stop_copy():
    n = 0
    for t in STATE["copy_tasks"].values():
        if t and not t.done():
            t.cancel()
            n += 1
    STATE["copy_tasks"] = {}
    return n


# backward-compat wrapper (used by /stopwatch)
def _stop_watch():
    return _stop_auto(), _stop_schedule()


@owner_only
async def cancelauto_cmd(update, context):
    n = _stop_auto()
    await update.effective_message.reply_text(
        f"Auto-mint stopped ({n} watcher(s)). Armed txs cleared." if n else "Auto-mint wasn't running.")


@owner_only
async def cancelschedule_cmd(update, context):
    await update.effective_message.reply_text(
        "Scheduled mint stopped." if _stop_schedule() else "No scheduled mint was set.")


@owner_only
async def cancelcopy_cmd(update, context):
    await update.effective_message.reply_text(
        "Copy-mint stopped." if _stop_copy() else "Copy-mint wasn't running.")


# aliases kept so old names still work
@owner_only
async def stopwatch_cmd(update, context):
    n = _stop_auto()
    had = _stop_schedule()
    bits = []
    if n:
        bits.append(f"{n} auto watcher(s)")
    if had:
        bits.append("scheduled mint")
    await update.effective_message.reply_text(
        ("Stopped " + " + ".join(bits) + " + cleared armed txs.") if bits else "Nothing was watching.")


@owner_only
async def stopcopy_cmd(update, context):
    await update.effective_message.reply_text("Copy-mint stopped." if _stop_copy() else "Copy-mint wasn't running.")


@owner_only
async def cancel_cmd(update, context):
    a = _stop_auto()
    s = _stop_schedule()
    c = _stop_copy()
    await update.effective_message.reply_text(
        f"Cancelled EVERYTHING - auto ({a}), schedule ({'yes' if s else 'no'}), copy ({c}). Armed txs cleared.")


# ------------------------------------------------------------------ wallet mgmt
def append_wallets(entries):
    data = []
    if os.path.exists(WALLETS_FILE):
        try:
            data = json.load(open(WALLETS_FILE))
        except Exception:
            data = []
    data.extend(entries)
    with open(WALLETS_FILE, "w") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(WALLETS_FILE, 0o600)  # keys: owner-read-only
    except Exception:
        pass


@owner_only
async def newwallet_cmd(update, context):
    n = 1
    if context.args:
        try:
            n = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    acctf = Web3().eth.account
    base = len(STATE["wallets"])
    entries, addrs = [], []
    for i in range(n):
        a = acctf.create()
        entries.append({"name": f"w{base+i+1}", "key": a.key.hex(), "qty": 1})
        addrs.append(a.address)
    append_wallets(entries)
    STATE["wallets"] = load_wallets()
    lines = [f"Created {n} wallet(s). Total now {len(STATE['wallets'])}. Fund each with ETH on RH Chain:"]
    lines += [f"{base+i+1}. {addr}" for i, addr in enumerate(addrs)]
    lines.append("Keys saved to wallets.json on this server - BACK IT UP. Lose the file, lose the wallets.")
    await update.effective_message.reply_text("\n".join(lines))


@owner_only
async def importwallet_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /importwallet 0xPRIVATEKEY")
        return
    key = context.args[0].strip()
    try:
        a = Web3().eth.account.from_key(key)
    except Exception:
        await update.effective_message.reply_text("Invalid private key.")
        return
    append_wallets([{"name": f"w{len(STATE['wallets'])+1}", "key": key, "qty": 1}])
    STATE["wallets"] = load_wallets()
    await update.effective_message.reply_text(
        f"Imported {a.address}. Total {len(STATE['wallets'])}.\n"
        "Now DELETE your /importwallet message - it contains the key and Telegram keeps history."
    )


@owner_only
async def setgas_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setgas <maxFeeGwei> [priorityGwei] [gasLimit]\n"
            "e.g. /setgas 0.1 0.01 500000   |   /setgas auto to reset"
        )
        return
    if context.args[0].lower() == "auto":
        STATE["gas_max_fee"] = STATE["gas_priority"] = STATE["gas_limit_override"] = None
        for w in STATE["wallets"]:
            w["armed"] = None
        await update.effective_message.reply_text("Gas back to auto.")
        return
    try:
        STATE["gas_max_fee"] = int(Web3.to_wei(Decimal(context.args[0]), "gwei"))
        if len(context.args) >= 2:
            STATE["gas_priority"] = int(Web3.to_wei(Decimal(context.args[1]), "gwei"))
        if len(context.args) >= 3:
            STATE["gas_limit_override"] = int(context.args[2])
    except Exception:
        await update.effective_message.reply_text("bad values")
        return
    for w in STATE["wallets"]:
        w["armed"] = None
    await update.effective_message.reply_text(
        f"Gas set: maxFee {context.args[0]} gwei. "
        "(Heads up: on this FCFS chain higher gas doesn't win the race - lowest latency does - "
        "but it's set as you asked.)"
    )


# ------------------------------------------------------------------ copy-mint
def fetch_txlist(target, n=5):
    """Explorer API - used only as a fallback / initial seed. Slow (indexer lag)."""
    url = f"{BLOCKSCOUT}?module=account&action=txlist&address={target}&sort=desc&page=1&offset={n}"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read().decode())
    if str(d.get("status")) == "1" and isinstance(d.get("result"), list):
        return d["result"]
    return []


def scan_blocks_for(_w3, targets_lower, from_block, to_block):
    """Read new blocks straight from the chain and pull out txs sent by any tracked
    wallet. Far faster than the explorer API (no indexer lag) and not rate-limited
    the same way. Returns (list_of_tx_dicts, last_block_scanned)."""
    found = []
    b = from_block
    while b <= to_block:
        try:
            blk = _w3.eth.get_block(b, full_transactions=True)
        except Exception:
            break
        for t in blk.transactions:
            frm = (t.get("from") or "")
            if frm and frm.lower() in targets_lower:
                data = t.get("input")
                if isinstance(data, (bytes, bytearray)):
                    data = "0x" + data.hex()
                data = data or "0x"
                h = t.get("hash")
                found.append({
                    "hash": h.hex() if hasattr(h, "hex") else str(h),
                    "from": frm,
                    "to": t.get("to"),
                    "input": data,
                    "value": str(t.get("value") or 0),
                })
        b += 1
    return found, min(b - 1, to_block)


def build_custom_tx(_w3, wal, to, data, value, nonce=None):
    acct = wal["account"]
    if nonce is None:
        nonce = _w3.eth.get_transaction_count(acct.address, "pending")
    max_fee, prio = suggest_fees(_w3)
    try:
        gas = int(_w3.eth.estimate_gas({"from": acct.address, "to": to, "data": data, "value": value}) * 1.25)
    except Exception:
        gas = STATE.get("gas_limit_override") or DEFAULT_GAS
    return {
        "chainId": CHAIN_ID, "to": to, "from": acct.address, "data": data, "value": value,
        "nonce": nonce, "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
    }


SIGNED_SELECTOR = "0x" + Web3.keccak(
    text="mintSigned(address,address,address,uint256,"
         "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
)[:4].hex().replace("0x", "").lower()
PUBLIC_SELECTOR = "0x" + Web3.keccak(
    text="mintPublic(address,address,address,uint256)"
)[:4].hex().replace("0x", "").lower()


def calldata_collection(data):
    """First arg of any SeaDrop mint is the nft contract."""
    body = data[2:] if data.startswith("0x") else data
    try:
        return Web3.to_checksum_address("0x" + body[8:72][24:])
    except Exception:
        return None


def is_signed_mint(data):
    body = data[2:] if data.startswith("0x") else data
    return ("0x" + body[:8].lower()) == SIGNED_SELECTOR


SELECTOR_NAME = {("0x" + Web3.keccak(text=s)[:4].hex().replace("0x", "").lower()): s
                 for s in MINT_SIGS}
STAGE_LABEL = {
    "0x161ac21f": "PUBLIC phase",
    "0x4b61cd6f": "WHITELIST phase (signature)",
    "0x4300a4e6": "ALLOWLIST phase (merkle proof)",
}


def describe_mint(_w3, to, data, value, qty_hint=None):
    """Work out what a watched wallet actually minted, for a readable alert."""
    sel = ("0x" + (data[2:10] if data.startswith("0x") else data[:8])).lower()
    is_sea = to.lower() == SEADROP_ADDR.lower()
    collection = (calldata_collection(data) or to) if is_sea else to
    info = {
        "collection": collection,
        "route": "OpenSea SeaDrop" if is_sea else "Direct contract",
        "phase": STAGE_LABEL.get(sel, SELECTOR_NAME.get(sel, sel)),
        "selector": sel,
        "name": None,
        "qty": qty_hint,
        "price_wei": value,
        "supply": None,
        "max_wallet": None,
    }
    try:
        info["name"] = read_name(_w3, collection)
    except Exception:
        pass
    # quantity sits in word 3 for SeaDrop mints
    if is_sea and qty_hint is None:
        try:
            body = data[2:] if data.startswith("0x") else data
            info["qty"] = int(body[8 + 64 * 3: 8 + 64 * 4], 16)
        except Exception:
            pass
    if is_sea:
        try:
            pd = read_public_drop(_w3, collection)
            if pd:
                info["max_wallet"] = int(pd[3])
                if value == 0 and int(pd[0]) > 0:
                    info["price_wei"] = None  # their stage was free, public may not be
        except Exception:
            pass
    return info


def format_mint(info):
    lines = []
    lines.append(f"Collection: {info['name']}" if info["name"] else "Collection: (unnamed)")
    lines.append(f"Contract: {info['collection']}")
    lines.append(f"Via: {info['route']} - {info['phase']}")
    bits = []
    if info.get("qty"):
        bits.append(f"qty {info['qty']}")
    if info.get("price_wei") is not None:
        p = Decimal(info["price_wei"]) / Decimal(10 ** 18)
        bits.append("FREE" if p == 0 else f"{p} ETH")
    if info.get("max_wallet"):
        bits.append(f"max {info['max_wallet']}/wallet")
    if bits:
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def copy_wallet_calldata(to, data, wal):
    # if the target used OpenSea SeaDrop mintPublic, rebuild with OUR wallet as minter
    body = data[2:] if data.startswith("0x") else data
    if (to.lower() == SEADROP_ADDR.lower()
            and body[:8].lower() == selector("mintPublic(address,address,address,uint256)")):
        collection = Web3.to_checksum_address("0x" + body[8:72][24:])
        return seadrop_calldata(collection, wal["address"], wal["qty"] or STATE["qty"])
    return data if data.startswith("0x") else "0x" + data


async def copy_confirm(context, chat_id, _w3, txh, name):
    try:
        r = await run_blocking(_w3.eth.wait_for_transaction_receipt, txh, 90)
        ok = r.get("status") == 1
        await context.bot.send_message(
            chat_id, f"[{name}] " + ("CONFIRMED" if ok else "reverted") + f" | block {r.get('blockNumber')}")
    except Exception:
        await context.bot.send_message(chat_id, f"[{name}] not confirmed in 90s - check the explorer")


async def copy_fire(context, chat_id, to, data, value):
    _w3 = w3()
    _sw = submit_w3()
    await context.bot.send_message(chat_id, f"Minting from {len(STATE['wallets'])} wallets -> {to[:10]}..")

    async def one(wal):
        try:
            cd = copy_wallet_calldata(to, data, wal)
            tx = await run_blocking(build_custom_tx, _w3, wal, to, cd, value)
            raw = sign_wallet(_w3, wal, tx)
            txh = await run_blocking(do_send, _sw, raw)
            await context.bot.send_message(chat_id, f"[{wal['name']}] minting.. SENT {txh}\n{EXPLORER}/tx/{txh}")
            asyncio.create_task(copy_confirm(context, chat_id, _w3, txh, wal["name"]))
        except Exception as e:
            return f"{wal['name']}: {safe(e, 60)}"
        return None

    fails = [f for f in await asyncio.gather(*[one(w) for w in STATE["wallets"]]) if f]
    if fails:
        head = fails[0].split(":", 1)[1].strip() if ":" in fails[0] else fails[0]
        await context.bot.send_message(
            chat_id,
            f"{len(fails)}/{len(STATE['wallets'])} wallets failed - {safe(head, 90)}")


async def _copy_loop(context, chat_id, target, interval):
    seen = set()
    _w3 = w3()
    tl = {target.lower()}
    try:
        last_block = await run_blocking(lambda: _w3.eth.block_number)
    except Exception:
        last_block = None
    backoff = interval
    await context.bot.send_message(
        chat_id,
        f"Copy-mint watching {copy_label(target)} ({target})\n"
        "Scanning new blocks directly (fast). FREE mints fire instantly; PAID mints ask for "
        "approval (buttons). Watching until /cancel. (No mempool here, so you follow the moment "
        "their tx lands - you can't pre-empt it.)"
    )
    while True:
        txs = []
        try:
            head = await run_blocking(lambda: _w3.eth.block_number)
            if last_block is None:
                last_block = head
            if head > last_block:
                # never scan more than 25 blocks in one pass - if we fell behind,
                # skip ahead rather than hammering the RPC with a huge catch-up
                start = last_block + 1
                if head - start > 25:
                    start = head - 25
                txs, last_block = await run_blocking(scan_blocks_for, _w3, tl, start, head)
            backoff = interval
        except Exception as e:
            # 429 / rate limit -> ease off instead of retrying at full speed
            if is_rate_limit(e):
                try:
                    mark_rate_limited(RPC_POOL[0])
                except Exception:
                    pass
                _w3 = w3()
                backoff = min(backoff * 2 if backoff else interval, 15.0)
                await asyncio.sleep(backoff)
            else:
                await asyncio.sleep(interval)
            continue
        for t in list(txs):
            h = t.get("hash")
            if not h or h in seen:
                continue
            seen.add(h)
            frm = (t.get("from") or "").lower()
            to = t.get("to")
            data = t.get("input") or "0x"
            if frm != target.lower() or not to:
                continue
            if not (data.startswith("0x") and len(data) >= 10 and data[:10].lower() in MINT_SELECTORS):
                continue
            to = Web3.to_checksum_address(to)
            val = int(t.get("value") or "0")

            # Signature-gated WL mint: the 65-byte signature is bound to THEIR
            # wallet, so replaying it always reverts. Try the collection's PUBLIC
            # stage instead (that one is copyable); otherwise just report it.
            if is_signed_mint(data):
                coll = calldata_collection(data)
                pd = None
                if coll:
                    try:
                        pd = await run_blocking(read_public_drop, _w3, coll)
                    except Exception:
                        pd = None
                live = False
                if pd:
                    now = int(time.time())
                    live = int(pd[3]) > 0 and int(pd[1]) <= now <= int(pd[2])
                if live:
                    try:
                        fee = await run_blocking(resolve_fee_recipient, _w3, coll)
                    except Exception:
                        fee = OPENSEA_FEE_RECIPIENT
                    price = int(pd[0])
                    pub_price = Decimal(price) / Decimal(10 ** 18)
                    try:
                        _info = await run_blocking(describe_mint, _w3, to, data, val)
                        _detail = format_mint(_info)
                    except Exception:
                        _detail = f"Contract: {coll}"
                    await context.bot.send_message(
                        chat_id,
                        f"{copy_label(target)} minted the WHITELIST phase (can't copy their signature)\n\n"
                        f"{_detail}\n\n"
                        f"But the PUBLIC phase is LIVE ({pub_price} ETH) - using that for your wallets.")
                    STATE["seadrop_fee"] = fee
                    pub_data = seadrop_calldata(coll, STATE["wallets"][0]["address"], 1) if STATE["wallets"] else None
                    if pub_data:
                        if price == 0:
                            asyncio.create_task(copy_fire(context, chat_id, SEADROP_ADDR, pub_data, 0))
                        else:
                            pid = str(STATE["pending_seq"]); STATE["pending_seq"] += 1
                            STATE["pending"][pid] = {"to": SEADROP_ADDR, "data": pub_data, "value": price,
                                                     "desc": f"{pub_price} ETH -> {coll[:10]}.. (public)"}
                            kb = InlineKeyboardMarkup([[
                                InlineKeyboardButton("Approve", callback_data=f"cp|a|{pid}"),
                                InlineKeyboardButton("Skip", callback_data=f"cp|s|{pid}")]])
                            await context.bot.send_message(
                                chat_id,
                                f"Public stage is PAID: {pub_price} ETH x {len(STATE['wallets'])} wallets "
                                f"= {pub_price * len(STATE['wallets'])} ETH. Mint?", reply_markup=kb)
                else:
                    try:
                        _info = await run_blocking(describe_mint, _w3, to, data, val)
                        _detail = format_mint(_info)
                    except Exception:
                        _detail = f"Contract: {coll or to}"
                    await context.bot.send_message(
                        chat_id,
                        f"{copy_label(target)} minted a WHITELIST drop - can't auto-copy\n\n"
                        f"{_detail}\n\n"
                        f"Their signature only works for their wallet, and no public stage is live yet.\n"
                        f"-> /oscheck to see if YOUR wallets are allowlisted\n"
                        f"-> or paste {(coll or to)} to watch for the public phase")
                continue

            if val == 0:
                try:
                    _info = await run_blocking(describe_mint, _w3, to, data, val)
                    _detail = format_mint(_info)
                except Exception:
                    _detail = f"Contract: {to}"
                await context.bot.send_message(
                    chat_id,
                    f"{copy_label(target)} minted (FREE) - copying to all wallets\n\n{_detail}")
                asyncio.create_task(copy_fire(context, chat_id, to, data, val))
            else:
                pid = str(STATE["pending_seq"])
                STATE["pending_seq"] += 1
                price = Decimal(val) / Decimal(10 ** 18)
                STATE["pending"][pid] = {"to": to, "data": data, "value": val, "desc": f"{price} ETH -> {to[:10]}.."}
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Approve", callback_data=f"cp|a|{pid}"),
                    InlineKeyboardButton("Skip", callback_data=f"cp|s|{pid}"),
                ]])
                try:
                    _info = await run_blocking(describe_mint, _w3, to, data, val)
                    _detail = format_mint(_info)
                except Exception:
                    _detail = f"Contract: {to}"
                await context.bot.send_message(
                    chat_id,
                    f"{copy_label(target)} minted a PAID drop\n\n{_detail}\n\n"
                    f"Your cost: {price} ETH x {len(STATE['wallets'])} wallets "
                    f"= {price * len(STATE['wallets'])} ETH\n"
                    f"Mint from your wallets?",
                    reply_markup=kb,
                )
        await asyncio.sleep(interval)


async def cb_handler(update, context):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    if (not OWNER_ID) or (not q.from_user) or q.from_user.id != OWNER_ID:
        return
    parts = (q.data or "").split("|")
    if len(parts) == 3 and parts[0] == "cp":
        action, pid = parts[1], parts[2]
        pend = STATE["pending"].pop(pid, None)
        if not pend:
            try:
                await q.edit_message_text("This request was already handled or expired.")
            except Exception:
                pass
            return
        if action == "s":
            try:
                await q.edit_message_text(f"Skipped: {pend['desc']}")
            except Exception:
                pass
            return
        try:
            await q.edit_message_text(f"Approved: {pend['desc']} - minting...")
        except Exception:
            pass
        await copy_fire(context, q.message.chat_id, pend["to"], pend["data"], pend["value"])
        return
    if len(parts) == 2 and parts[0] == "act":
        a = parts[1]
        if a == "sim":
            await simall_cmd(update, context)
        elif a == "arm":
            await armall_cmd(update, context)
        elif a == "mint":
            await mintall_cmd(update, context)
        elif a == "auto":
            await autoall_cmd(update, context)
        return
    if parts[0] == "osx":
        act, stage = parts[1], parts[2]
        if act == "r":
            ctx = STATE.get("os_ctx")
            if ctx:
                STATE["os_sessions"] = {}
                await os_autoload(update, context, ctx["slug"])
            return
        if act == "m":
            await os_fire_stage(context, q.message.chat_id, stage)
            return
        if act == "a":
            old = STATE["auto_tasks"].get("_os")
            if old and not old.done():
                old.cancel()
            STATE["auto_tasks"]["_os"] = asyncio.create_task(
                os_auto_stage(context, q.message.chat_id, stage))
            return
    if parts[0] == "cw":
        if len(parts) == 2 and parts[1] == "add":
            STATE["awaiting_copy"] = True
            await context.bot.send_message(
                q.message.chat_id,
                "Send the wallet address to copy-mint (paste the 0x address). /cancel to abort.")
            return
        if len(parts) == 3 and parts[1] == "rm":
            addr = parts[2]
            t = STATE["copy_tasks"].pop(addr, None)
            if t and not t.done():
                t.cancel()
            try:
                await q.edit_message_text(copywatch_text(), reply_markup=copywatch_kb())
            except Exception:
                pass
            return


def copy_label(addr):
    """Friendly name for a tracked wallet, falling back to a short address."""
    lbl = STATE["copy_labels"].get(addr)
    return lbl if lbl else f"{addr[:8]}..{addr[-4:]}"


async def add_copy_target(context, chat_id, target, interval=1.0, label=None):
    existing = STATE["copy_tasks"].get(target)
    if existing and not existing.done():
        await context.bot.send_message(chat_id, f"Already tracking {target[:10]}..")
        return
    if label:
        STATE["copy_labels"][target] = label
    STATE["copy_tasks"][target] = asyncio.create_task(_copy_loop(context, chat_id, target, interval))
    await context.bot.send_message(
        chat_id,
        f"Added to copy watchlist: {copy_label(target)}\n{target}\n"
        f"Now tracking {len(STATE['copy_tasks'])} wallet(s). /copywatch to manage.")


def copywatch_text():
    active = {a: t for a, t in STATE["copy_tasks"].items() if t and not t.done()}
    if not active:
        return "Copy-mint watchlist: empty.\nTap + Add below, or send /copy 0xADDRESS."
    lines = [f"Copy-mint watchlist ({len(active)}):"]
    for a in active:
        lbl = STATE["copy_labels"].get(a)
        lines.append(f" - {lbl}\n   {a}" if lbl else f" - {a}")
    return "\n".join(lines)


def copywatch_kb():
    rows = []
    for a, t in STATE["copy_tasks"].items():
        if t and not t.done():
            rows.append([InlineKeyboardButton(f"Remove {copy_label(a)}", callback_data=f"cw|rm|{a}")])
    rows.append([InlineKeyboardButton("+ Add wallet", callback_data="cw|add")])
    return InlineKeyboardMarkup(rows)


@owner_only
async def copywatch_cmd(update, context):
    await update.effective_message.reply_text(copywatch_text(), reply_markup=copywatch_kb())


@owner_only
async def copy_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /copy 0xADDRESS [name]\n"
            "e.g. /copy 0xabc... whale1\n"
            "The name shows in alerts so you know whose mint it is. /rename to change it.")
        return
    try:
        target = Web3.to_checksum_address(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Invalid address.")
        return
    label = " ".join(context.args[1:]).strip() or None
    await add_copy_target(context, update.effective_chat.id, target, 1.0, label)


@owner_only
async def rename_cmd(update, context):
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /rename 0xADDRESS <name>   (use /rename 0xADDRESS - to clear)")
        return
    try:
        addr = Web3.to_checksum_address(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Invalid address.")
        return
    name = " ".join(context.args[1:]).strip()
    if name == "-":
        STATE["copy_labels"].pop(addr, None)
        await update.effective_message.reply_text(f"Name cleared for {addr[:10]}..")
        return
    STATE["copy_labels"][addr] = name
    await update.effective_message.reply_text(f"Named {addr[:10]}.. as \"{name}\"")


@owner_only
async def mintloop_cmd(update, context):
    """Fire N mint txs per wallet back-to-back (for drops with a high per-wallet cap
    but a low per-transaction limit)."""
    if not STATE["contract"]:
        await update.effective_message.reply_text("Run /source first.")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /mintloop <count> [delayMs]\n"
            "Sends <count> mint txs from EVERY wallet, one after another.\n"
            "e.g. /mintloop 10  -> 10 mints per wallet. /cancelauto to stop early.\n"
            "Note: each tx costs gas - check the drop is worth it first.")
        return
    try:
        count = max(1, min(1000, int(context.args[0])))
    except ValueError:
        await update.effective_message.reply_text("Bad count.")
        return
    delay = 0.15
    if len(context.args) >= 2:
        try:
            delay = max(0.0, float(context.args[1]) / 1000.0)
        except ValueError:
            pass
    total = count * len(STATE["wallets"])
    await update.effective_message.reply_text(
        f"Mint loop: {count} tx per wallet x {len(STATE['wallets'])} wallets = {total} txs.\n"
        f"Each costs gas. Running... /cancelauto to stop.")

    async def loop_one(wal):
        _w3 = wallet_w3(wal)
        _sw = _w3
        ok = fail = 0
        try:
            nonce = await run_blocking(
                lambda: _w3.eth.get_transaction_count(wal["address"], "pending"))
        except Exception as e:
            return f"[{wal['name']}] nonce error: {safe(e, 50)}"
        for i in range(count):
            try:
                tx = await run_blocking(build_wallet_tx, _w3, wal, nonce)
                raw = sign_wallet(_w3, wal, tx)
                await run_blocking(do_send, _sw, raw)
                ok += 1
                nonce += 1
            except Exception as e:
                fail += 1
                msg = str(e).lower()
                # stop this wallet early on a hard stop (cap hit / out of funds)
                if any(k in msg for k in ("exceed", "max", "insufficient", "limit")):
                    return f"[{wal['name']}] {ok} sent, stopped at #{i+1}: {safe(e, 60)}"
                if fail >= 3:
                    return f"[{wal['name']}] {ok} sent, aborted after 3 errors: {safe(e, 50)}"
            if delay:
                await asyncio.sleep(delay)
        return f"[{wal['name']}] {ok}/{count} sent" + (f", {fail} failed" if fail else "")

    tasks = {w["name"]: asyncio.create_task(loop_one(w)) for w in STATE["wallets"]}
    STATE["auto_tasks"] = tasks  # so /cancelauto can stop the loop
    res = await asyncio.gather(*tasks.values(), return_exceptions=True)
    STATE["auto_tasks"] = {}
    out = [str(r) if not isinstance(r, Exception) else f"error: {str(r)[:60]}" for r in res]
    await update.effective_message.reply_text("\n".join(["Mint loop done:"] + out))



def os_parse_time(v):
    """Stage times come back as epoch seconds or ISO8601. Return epoch float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.fullmatch(r"\d{10}", s):
        return float(s)
    if re.fullmatch(r"\d{13}", s):
        return float(s) / 1000.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def os_stage_label(t):
    return {"SIGNED_PRESALE": "WHITELIST", "MERKLE_PRESALE": "ALLOWLIST",
            "PUBLIC_SALE": "PUBLIC"}.get(t, t)


def os_ctx_buttons():
    """One row per phase: MINT NOW + AUTO (fires at the phase's start time)."""
    ctx = STATE.get("os_ctx") or {}
    rows = []
    for t in ctx.get("order", []):
        n = len(ctx["elig"].get(t, []))
        if not n:
            continue
        lbl = os_stage_label(t)
        rows.append([
            InlineKeyboardButton(f"MINT {lbl} NOW ({n})", callback_data=f"osx|m|{t}"),
            InlineKeyboardButton(f"AUTO {lbl}", callback_data=f"osx|a|{t}"),
        ])
    rows.append([InlineKeyboardButton("Refresh eligibility", callback_data="osx|r|-")])
    return InlineKeyboardMarkup(rows)


async def os_autoload(update, context, slug):
    """Paste an OpenSea link -> log in, read stages + per-wallet eligibility, show buttons."""
    reply = update.effective_message.reply_text
    _w3 = w3()
    try:
        chain_id = await run_blocking(lambda: _w3.eth.chain_id)
    except Exception:
        chain_id = CHAIN_ID
    ws = STATE["wallets"]
    todo = [w for w in ws if w["name"] not in STATE["os_sessions"]]
    est = int(len(todo) * OS_DELAY / max(1, OS_CONCURRENCY)) + 3
    msg = await reply(f"Loading {slug} - checking {len(ws)} wallets"
                      + (f" (~{est}s)" if todo else " (cached)") + "...")

    # sessions first (needed for every later call)
    async def login(w):
        try:
            if w["name"] not in STATE["os_sessions"]:
                STATE["os_sessions"][w["name"]] = await os_retry(
                    os_login, w, slug, chain_id, label=w["name"])
            return None
        except Exception as e:
            return f"{w['name']}: {safe(e, 40)}"

    login_errs = [e for e in await asyncio.gather(*[login(w) for w in ws]) if e]
    live = [w for w in ws if w["name"] in STATE["os_sessions"]]
    if not live:
        await reply("Couldn't log any wallet into OpenSea. Try again in a minute (/oslogout to reset).")
        return

    try:
        coll = await os_retry(os_collection, STATE["os_sessions"][live[0]["name"]], slug)
    except Exception as e:
        await reply(f"Collection lookup failed: {safe(e, 90)}")
        return

    stage_meta = {}
    for st in coll.get("stages") or []:
        t = st.get("stageType")
        if not t:
            continue
        stage_meta[t] = {
            "start": os_parse_time(st.get("startTime")),
            "end": os_parse_time(st.get("endTime")),
            "index": st.get("stageIndex"),
        }

    async def elig(w):
        try:
            stages, _ = await os_retry(os_eligibility, STATE["os_sessions"][w["name"]], slug, w,
                                       label=w["name"])
            return w["name"], [s for s in stages if s["eligible"]], None
        except Exception as e:
            return w["name"], [], safe(e, 40)

    results = await asyncio.gather(*[elig(w) for w in live])
    elig_map, price_map, max_map, errs = {}, {}, {}, []
    for name, stages, err in results:
        if err:
            errs.append(f"{name}: {err}")
        for s in stages:
            t = s["type"]
            elig_map.setdefault(t, []).append(name)
            if s.get("price_wei") is not None:
                price_map[t] = s["price_wei"]
            if s.get("max"):
                max_map[t] = max(int(s["max"] or 0), max_map.get(t, 0))

    order = [t for t in ("SIGNED_PRESALE", "MERKLE_PRESALE", "PUBLIC_SALE") if t in elig_map]
    order += [t for t in elig_map if t not in order]
    STATE["os_ctx"] = {"slug": slug, "coll": coll, "elig": elig_map, "price": price_map,
                       "max": max_map, "meta": stage_meta, "order": order, "chain_id": chain_id}

    lines = [f"{coll.get('name') or slug}", f"Contract: {coll['address']}",
             f"Wallets checked: {len(live)}/{len(ws)}"]
    if not order:
        lines.append("\nNo eligible phase for any wallet right now.")
    now = time.time()
    for t in order:
        p = price_map.get(t)
        pe = "?" if p is None else ("FREE" if p == 0 else f"{Decimal(p)/Decimal(10**18)} ETH")
        m = stage_meta.get(t, {})
        when = ""
        if m.get("start"):
            if m["start"] > now:
                when = f" | opens in {_fmt_eta(m['start'])}"
            elif m.get("end") and m["end"] < now:
                when = " | ENDED"
            else:
                when = " | LIVE NOW"
        lines.append(f"\n{os_stage_label(t)}: {len(elig_map[t])} wallets eligible | "
                     f"max {max_map.get(t, '?')}/wallet | {pe}{when}")
    if errs or login_errs:
        bad = (login_errs + errs)[:3]
        lines.append(f"\n({len(login_errs)+len(errs)} wallet(s) errored - tap Refresh: {bad[0]})")
    await reply("\n".join(lines), reply_markup=os_ctx_buttons())


async def os_fire_stage(context, chat_id, stage_type, qty=None, only=None):
    """Fetch each eligible wallet's own calldata from OpenSea, validate, and blast it."""
    ctx = STATE.get("os_ctx")
    if not ctx:
        await context.bot.send_message(chat_id, "Load a collection first (paste the OpenSea link).")
        return
    names = ctx["elig"].get(stage_type) or []
    wallets = only if only else [w for w in STATE["wallets"] if w["name"] in names]
    if not wallets:
        await context.bot.send_message(chat_id, f"No wallets eligible for {os_stage_label(stage_type)}.")
        return
    per = qty or STATE.get("os_qty") or ctx["max"].get(stage_type) or 1
    per = max(1, int(per))
    price = ctx["price"].get(stage_type) or 0
    total = (Decimal(price) * per * len(wallets)) / Decimal(10 ** 18)
    if total > MAX_SPEND_ETH:
        await context.bot.send_message(
            chat_id, f"SPEND GUARD: {total} ETH needed, limit is {MAX_SPEND_ETH}. "
                     f"Raise RH_MAX_SPEND_ETH or lower qty with /osqty.")
        return
    await context.bot.send_message(
        chat_id, f"FIRING {os_stage_label(stage_type)} - {len(wallets)} wallets x{per} "
                 f"({total} ETH total)")
    coll, chain_id = ctx["coll"], ctx["chain_id"]

    async def one(wal):
        try:
            s = STATE["os_sessions"].get(wal["name"])
            if s is None:
                s = await os_retry(os_login, wal, ctx["slug"], chain_id, label=wal["name"])
                STATE["os_sessions"][wal["name"]] = s
            action = None
            last = None
            for _ in range(6):                    # phase may flip live a beat late
                try:
                    action = await os_retry(os_mint_action, s, coll, wal, per, label=wal["name"])
                    break
                except Exception as e:
                    last = e
                    if is_os_rate_limited(e):
                        raise
                    await asyncio.sleep(0.5)
            if action is None:
                raise last or RuntimeError("no mint action")
            await run_blocking(os_validate, action, coll, wal, stage_type, per, chain_id)
            _pw3, _ = wallet_rpc(wal)
            tx = await run_blocking(build_custom_tx, _pw3, wal, action["to"],
                                    action["data"], action["value"])
            raw = sign_wallet(_pw3, wal, tx)
            txh = await run_blocking(do_send, _pw3, raw)
            await context.bot.send_message(chat_id, f"[{wal['name']}] SENT {txh}\n{EXPLORER}/tx/{txh}")
            asyncio.create_task(copy_confirm(context, chat_id, _pw3, txh, wal["name"]))
            STATE.setdefault("os_done", {})[wal["name"]] = True
            return None
        except Exception as e:
            return f"{wal['name']}: {safe(e, 60)}"

    fails = [f for f in await asyncio.gather(*[one(w) for w in wallets]) if f]
    if fails:
        await context.bot.send_message(
            chat_id, f"{len(fails)}/{len(wallets)} failed:\n" + "\n".join(fails[:6]))
    else:
        await context.bot.send_message(chat_id, f"All {len(wallets)} wallets fired.")


async def os_prearm(context, chat_id, stage_type, per, wallets, coll, chain_id, slug):
    """Try to get each wallet's signed calldata BEFORE the phase opens, and pre-sign the
    tx. At T+0 we then only broadcast - no API round trip in the critical path.
    OpenSea often issues the signature early because validity is enforced on-chain."""
    armed = {}

    async def one(wal):
        try:
            s = STATE["os_sessions"].get(wal["name"])
            if s is None:
                s = await os_retry(os_login, wal, slug, chain_id, label=wal["name"])
                STATE["os_sessions"][wal["name"]] = s
            action = await os_retry(os_mint_action, s, coll, wal, per, label=wal["name"])
            await run_blocking(os_validate, action, coll, wal, stage_type, per, chain_id)
            _pw3, _ = wallet_rpc(wal)
            tx = await run_blocking(build_custom_tx, _pw3, wal, action["to"],
                                    action["data"], action["value"])
            armed[wal["name"]] = (sign_wallet(_pw3, wal, tx), _pw3)
            return None
        except Exception as e:
            return f"{wal['name']}: {safe(e, 40)}"

    errs = [e for e in await asyncio.gather(*[one(w) for w in wallets]) if e]
    return armed, errs


async def os_blast_armed(context, chat_id, armed):
    """Broadcast every pre-signed tx at once - this is the zero-latency path."""
    async def fire(name, pair):
        raw, _pw3 = pair
        try:
            txh = await run_blocking(do_send, _pw3, raw)
            await context.bot.send_message(chat_id, f"[{name}] SENT {txh}\n{EXPLORER}/tx/{txh}")
            asyncio.create_task(copy_confirm(context, chat_id, _pw3, txh, name))
            return None
        except Exception as e:
            return f"{name}: {safe(e, 60)}"
    return [f for f in await asyncio.gather(*[fire(n, p) for n, p in armed.items()]) if f]


async def os_auto_stage(context, chat_id, stage_type, qty=None):
    """Sleep until the phase opens (zero RPC), PRE-ARM shortly before, then blast at T+0."""
    ctx = STATE.get("os_ctx")
    if not ctx:
        return
    meta = ctx["meta"].get(stage_type) or {}
    start = meta.get("start")
    names = ctx["elig"].get(stage_type) or []
    wallets = only if only else [w for w in STATE["wallets"] if w["name"] in names]
    if not wallets:
        await context.bot.send_message(chat_id, f"No wallets eligible for {os_stage_label(stage_type)}.")
        return
    per = qty or STATE.get("os_qty") or ctx["max"].get(stage_type) or 1
    per = max(1, int(per))
    price = ctx["price"].get(stage_type) or 0
    total = (Decimal(price) * per * len(wallets)) / Decimal(10 ** 18)
    if total > MAX_SPEND_ETH:
        await context.bot.send_message(
            chat_id, f"SPEND GUARD: {total} ETH needed, limit {MAX_SPEND_ETH}.")
        return
    lead = float(os.environ.get("OS_PREARM_LEAD", "25") or "25")

    if start and start > time.time():
        await context.bot.send_message(
            chat_id,
            f"AUTO-MINT armed for {os_stage_label(stage_type)}\n"
            f"Opens in {_fmt_eta(start)} ({datetime.fromtimestamp(start, tz=timezone.utc):%H:%M:%S UTC})\n"
            f"Will pre-fetch signatures {int(lead)}s early, then broadcast at T+0.\n"
            f"Sleeping - no RPC used. /cancelauto to stop.")
        while True:
            left = start - time.time() - lead
            if left <= 0:
                break
            await asyncio.sleep(min(left, 20))

    # --- pre-arm attempt (the fix: get signatures BEFORE the gun) ---
    armed, errs = {}, []
    if start and start > time.time():
        armed, errs = await os_prearm(context, chat_id, stage_type, per, wallets,
                                      ctx["coll"], ctx["chain_id"], ctx["slug"])
        if armed:
            await context.bot.send_message(
                chat_id, f"PRE-ARMED {len(armed)}/{len(wallets)} wallets - "
                         f"broadcasting the instant it opens ({_fmt_eta(start)}).")
        else:
            await context.bot.send_message(
                chat_id, "OpenSea won't sign before open - will fetch+fire at T+0."
                         + (f" ({errs[0]})" if errs else ""))
        # wait out the remaining time to the exact open
        while True:
            left = start - time.time()
            if left <= 0.05:
                break
            await asyncio.sleep(min(left, 0.5) if left < 2 else min(left - 1, 10))

    if armed:
        fails = await os_blast_armed(context, chat_id, armed)
        missing = [w for w in wallets if w["name"] not in armed]
        if fails:
            await context.bot.send_message(chat_id, f"{len(fails)} broadcast failed:\n"
                                                    + "\n".join(fails[:5]))
        if missing:
            await os_fire_stage(context, chat_id, stage_type, per, only=missing)
    else:
        await os_fire_stage(context, chat_id, stage_type, per)

    # --- supply can free up from failed txs in the first seconds: keep trying briefly ---
    retry_s = float(os.environ.get("OS_RETRY_WINDOW", "8") or "8")
    if retry_s > 0:
        deadline = time.time() + retry_s
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            left = [w for w in wallets if not STATE.get("os_done", {}).get(w["name"])]
            if not left:
                break
    STATE["auto_tasks"].pop("_os", None)


@owner_only
async def osqty_cmd(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            f"Usage: /osqty <n>  (per wallet; currently "
            f"{STATE.get('os_qty') or 'auto = stage max'})")
        return
    try:
        STATE["os_qty"] = max(1, int(context.args[0]))
    except ValueError:
        await update.effective_message.reply_text("bad number")
        return
    await update.effective_message.reply_text(f"OpenSea mint qty per wallet: {STATE['os_qty']}")


@owner_only
async def oscheck_cmd(update, context):
    """Per-wallet eligibility across every phase (WL / FCFS / public) via OpenSea."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /oscheck <opensea collection link or slug>\n"
            "Logs each wallet into OpenSea (SIWE) and reports which phases it's eligible for.")
        return
    slug = os_slug(" ".join(context.args))
    if not slug:
        await update.effective_message.reply_text("Give an OpenSea collection link or slug.")
        return
    _w3 = w3()
    try:
        chain_id = await run_blocking(lambda: _w3.eth.chain_id)
    except Exception:
        chain_id = CHAIN_ID
    todo = [w for w in STATE["wallets"] if w["name"] not in STATE["os_sessions"]]
    est = int(len(todo) * OS_DELAY / max(1, OS_CONCURRENCY)) + 3
    await update.effective_message.reply_text(
        f"Checking {len(STATE['wallets'])} wallets on OpenSea "
        f"({len(todo)} need login, ~{est}s - throttled to avoid rate limits)...")

    async def one(wal):
        try:
            s = STATE["os_sessions"].get(wal["name"])
            if s is None:
                s = await os_retry(os_login, wal, slug, chain_id, label=wal["name"])
                STATE["os_sessions"][wal["name"]] = s
            stages, minted = await os_retry(os_eligibility, s, slug, wal, label=wal["name"])
            elig = [st for st in stages if st["eligible"]]
            if not elig:
                return f"[{wal['name']}] no eligible phase"
            bits = []
            for st in elig:
                p = (Decimal(st["price_wei"]) / Decimal(10 ** 18)) if st["price_wei"] is not None else "?"
                bits.append(f"{st['type']}(idx{st['index']}) max {st['max']} @ {p} ETH")
            return f"[{wal['name']}] " + " | ".join(bits)
        except Exception as e:
            if is_os_rate_limited(e):
                return f"[{wal['name']}] rate limited by OpenSea - re-run /oscheck"
            return f"[{wal['name']}] {safe(e, 70)}"

    res = await asyncio.gather(*[one(w) for w in STATE["wallets"]])
    STATE["os_slug"] = slug
    await update.effective_message.reply_text(
        "\n".join([f"OpenSea eligibility - {slug}:"] + list(res) +
                  ["", "Mint an eligible phase with: /osmint <STAGE_TYPE> [qty]",
                   "e.g. /osmint SIGNED_PRESALE 1   |  /osmint PUBLIC_SALE 2"]))


@owner_only
async def osmint_cmd(update, context):
    """Fetch per-wallet mint calldata from OpenSea (signature included for WL) and fire it."""
    slug = STATE.get("os_slug")
    if not slug:
        await update.effective_message.reply_text("Run /oscheck <collection> first.")
        return
    stage_type = (context.args[0].upper() if context.args else "PUBLIC_SALE")
    if stage_type not in STAGE_SELECTOR:
        await update.effective_message.reply_text(
            "Stage must be one of: SIGNED_PRESALE, MERKLE_PRESALE, PUBLIC_SALE")
        return
    qty = 1
    if len(context.args) >= 2:
        try:
            qty = max(1, int(context.args[1]))
        except ValueError:
            pass
    _w3 = w3()
    try:
        chain_id = await run_blocking(lambda: _w3.eth.chain_id)
    except Exception:
        chain_id = CHAIN_ID
    try:
        base = STATE["os_sessions"].get(STATE["wallets"][0]["name"])
        if base is None:
            base = await run_blocking(os_login, STATE["wallets"][0], slug, chain_id)
            STATE["os_sessions"][STATE["wallets"][0]["name"]] = base
        coll = await run_blocking(os_collection, base, slug)
    except Exception as e:
        await update.effective_message.reply_text(f"Collection lookup failed: {safe(e, 100)}")
        return
    await update.effective_message.reply_text(
        f"{slug} | {stage_type} | qty {qty}\nRequesting per-wallet calldata from OpenSea...")

    async def one(wal):
        try:
            s = STATE["os_sessions"].get(wal["name"])
            if s is None:
                s = await os_retry(os_login, wal, slug, chain_id, label=wal["name"])
                STATE["os_sessions"][wal["name"]] = s
            action = await os_retry(os_mint_action, s, coll, wal, qty, label=wal["name"])
            await run_blocking(os_validate, action, coll, wal, stage_type, qty, chain_id)
            _pw3 = wallet_w3(wal)
            tx = await run_blocking(build_custom_tx, _pw3, wal, action["to"],
                                    action["data"], action["value"])
            raw = sign_wallet(_pw3, wal, tx)
            txh = await run_blocking(do_send, _pw3, raw)
            asyncio.create_task(_confirm(update, _pw3, txh, wal["name"]))
            return f"[{wal['name']}] SENT {txh[:20]}.."
        except Exception as e:
            if is_os_rate_limited(e):
                return f"[{wal['name']}] OpenSea rate limited - retry /osmint"
            return f"[{wal['name']}] {safe(e, 70)}"

    res = await asyncio.gather(*[one(w) for w in STATE["wallets"]])
    await update.effective_message.reply_text("\n".join([f"OS mint ({stage_type}):"] + list(res)))


@owner_only
async def oslogout_cmd(update, context):
    n = len(STATE["os_sessions"])
    STATE["os_sessions"] = {}
    STATE["os_slug"] = None
    await update.effective_message.reply_text(
        f"Cleared {n} OpenSea session(s). Run /oscheck to log in again.")



# ---------------------------------------------------------------------
# Merkle-proof mints: pull each wallet's own proof from the project's API
# and build its calldata. Works for any site that serves proofs by address.
# ---------------------------------------------------------------------
def _find_proof(obj):
    """Recursively locate a list of 32-byte hex strings (the merkle proof)."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", x.strip())
                       for x in obj):
            return [x.strip() for x in obj]
        for it in obj:
            r = _find_proof(it)
            if r:
                return r
    elif isinstance(obj, dict):
        for k in ("proof", "proofs", "merkleProof", "merkle_proof", "hexProof"):
            if k in obj:
                r = _find_proof(obj[k])
                if r:
                    return r
        for v in obj.values():
            r = _find_proof(v)
            if r:
                return r
    return None


def _find_amount(obj):
    """Locate the per-wallet allowance (second arg of the mint call)."""
    keys = ("maxallowed", "max", "amount", "allowance", "limit", "quantity", "qty",
            "maxmint", "maxamount", "allocation", "tier")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower().replace("_", "") in keys:
                try:
                    return int(v)
                except Exception:
                    pass
        for v in obj.values():
            r = _find_amount(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_amount(it)
            if r is not None:
                return r
    return None


def fetch_proof(url_tpl, address):
    import requests
    url = url_tpl.replace("{address}", address).replace("{ADDRESS}", address) \
                 .replace("{addr}", address).replace("{wallet}", address)
    if "{" not in url_tpl:
        url = url_tpl.rstrip("/") + "/" + address
    r = requests.get(url, timeout=15, headers={
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    if r.status_code == 404:
        raise RuntimeError("not on allowlist (404)")
    r.raise_for_status()
    j = r.json()
    proof = _find_proof(j)
    if not proof:
        raise RuntimeError("no proof in response")
    return proof, _find_amount(j)


def build_proof_calldata(selector, qty, max_allowed, proof):
    sel = selector if selector.startswith("0x") else "0x" + selector
    enc = abi_encode(["uint256", "uint256", "bytes32[]"],
                     [int(qty), int(max_allowed),
                      [bytes.fromhex(p[2:]) for p in proof]]).hex()
    return sel + enc


@owner_only
async def proofapi_cmd(update, context):
    """/proofapi <url with {address}> [selector] [qty] - pull every wallet's proof and arm it."""
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /proofapi <url> [selector] [qty]\n\n"
            "Put {address} where the wallet goes, e.g.\n"
            "/proofapi https://site.com/api/proof?address={address}\n\n"
            "Find it: open the mint site, DevTools > Network, connect a wallet - "
            "look for the request that returns a list of 0x... hashes.\n"
            "Default selector 0xda39f8cf mint(qty,maxAllowed,proof[]).")
        return
    url_tpl = context.args[0]
    selector = context.args[1] if len(context.args) > 1 else "0xda39f8cf"
    qty = int(context.args[2]) if len(context.args) > 2 else 1
    await update.effective_message.reply_text(
        f"Fetching proofs for {len(STATE['wallets'])} wallets...")

    async def one(wal):
        try:
            proof, amt = await run_blocking(fetch_proof, url_tpl, wal["address"])
            max_allowed = amt if amt is not None else qty
            wal["calldata"] = build_proof_calldata(selector, qty, max_allowed, proof)
            wal["armed"] = None
            return f"[{wal['name']}] OK proof({len(proof)}) max {max_allowed}"
        except Exception as e:
            wal["calldata"] = ""
            return f"[{wal['name']}] {safe(e, 50)}"

    res = await asyncio.gather(*[one(w) for w in STATE["wallets"]])
    ok = sum(1 for w in STATE["wallets"] if w["calldata"])
    await update.effective_message.reply_text(
        "\n".join(list(res) + ["", f"{ok}/{len(STATE['wallets'])} wallets armed with proofs.",
                               "Now: /source <contract> -> /simall -> /armall -> Auto-mint"]))


@owner_only
async def setproof_cmd(update, context):
    """Manually set one wallet's calldata: /setproof w3 0x...."""
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /setproof <walletName> 0x<calldata>")
        return
    name, cd = context.args[0], context.args[1].strip()
    if not cd.startswith("0x"):
        cd = "0x" + cd
    for wal in STATE["wallets"]:
        if wal["name"].lower() == name.lower():
            wal["calldata"] = cd
            wal["armed"] = None
            await update.effective_message.reply_text(
                f"[{wal['name']}] calldata set ({len(cd[2:])//2} bytes).")
            return
    await update.effective_message.reply_text(f"No wallet named {name}. /wallets to list.")


@owner_only
async def rpcstatus_cmd(update, context):
    n = len(RPC_POOL)
    lines = [f"RPC pool: {n} endpoint(s), {len(healthy_endpoints())} healthy | "
             f"{len(STATE['wallets'])} wallets | max spend {MAX_SPEND_ETH} ETH"]

    async def chk(i):
        url = RPC_POOL[i]
        host = url.split("/")[2] if "//" in url else url
        tag = url.rstrip("/").rsplit("/", 1)[-1][:6]
        cool = _EP_COOL.get(url, 0) - time.time()
        if cool > 0:
            return f"[{i+1}] COOLING {int(cool)}s (rate limited) | ...{tag}"
        try:
            bn = await run_blocking(lambda: w3_for(url).eth.block_number)
            return f"[{i+1}] OK block {bn} | ...{tag}"
        except Exception as e:
            mark_rate_limited(url, 30) if is_rate_limit(e) else None
            return f"[{i+1}] DOWN {safe(e, 40)} | ...{tag}"

    res = await asyncio.gather(*[chk(i) for i in range(n)])
    await update.effective_message.reply_text("\n".join(lines + list(res)))


async def guard_msg(update, context):
    if not update.message or not update.message.text:
        return
    if (not OWNER_ID) or (not update.effective_user) or update.effective_user.id != OWNER_ID:
        return
    text = update.message.text.strip()
    if PK_PATTERN.search(text):
        await update.message.reply_text(
            "WARNING: that looks like a private key. DELETE that message now. "
            "Keys go in wallets.json on your server, never in chat.")
        return
    if STATE.get("awaiting_copy"):
        m = re.search(r"0x[a-fA-F0-9]{40}", text)
        if not m:
            await update.message.reply_text("Not a valid address. Paste a 0x wallet address, or /cancel.")
            return
        STATE["awaiting_copy"] = False
        await add_copy_target(context, update.effective_chat.id, Web3.to_checksum_address(m.group(0)))
        return
    _s = None
    if "opensea.io/collection/" in text:
        _s = os_slug(text)
    if _s:
        await os_autoload(update, context, _s)
        return
    if re.search(r"0x[a-fA-F0-9]{40}", text) or "opensea.io" in text:
        await do_source(update, context, text)
        return
    await update.message.reply_text("Paste a contract address or OpenSea link to load it, or /help.")


# ------------------------------------------------------------------ main
def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or AUTHORIZED_USER_ID - see .env.example")
    try:
        STATE["wallets"] = load_wallets()
    except Exception as e:
        raise SystemExit(f"Bad wallet key in {WALLETS_FILE}: {e}")
    if not STATE["wallets"]:
        raise SystemExit(f"No wallets. Create {WALLETS_FILE} (see wallets.json.example) or set RH_PRIVATE_KEY.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    for name, fn in [
        ("start", start_cmd), ("help", help_cmd), ("wallets", wallets_cmd),
        ("source", source_cmd), ("setfn", setfn_cmd), ("setqty", setqty_cmd),
        ("setvalue", setvalue_cmd), ("status", status_cmd), ("simall", simall_cmd),
        ("armall", armall_cmd), ("mintall", mintall_cmd), ("autoall", autoall_cmd),
        ("scheduleall", scheduleall_cmd), ("cancel", cancel_cmd),
        ("newwallet", newwallet_cmd), ("importwallet", importwallet_cmd),
        ("setgas", setgas_cmd), ("copy", copy_cmd),
        ("stopwatch", stopwatch_cmd), ("stopcopy", stopcopy_cmd),
        ("copywatch", copywatch_cmd), ("rpcstatus", rpcstatus_cmd),
        ("cancelauto", cancelauto_cmd), ("cancelschedule", cancelschedule_cmd),
        ("cancelcopy", cancelcopy_cmd), ("mintloop", mintloop_cmd),
        ("rename", rename_cmd), ("oscheck", oscheck_cmd), ("osmint", osmint_cmd), ("oslogout", oslogout_cmd), ("osqty", osqty_cmd), ("proofapi", proofapi_cmd), ("setproof", setproof_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guard_msg))

    log.info("Bot up. owner=%s chain=%s wallets=%d endpoints=%d",
             OWNER_ID, CHAIN_ID, len(STATE["wallets"]), len(RPC_POOL))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
