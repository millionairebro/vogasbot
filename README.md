# rh-sniper — smart terminal FCFS mint sniper for Robinhood Chain

Telegram-free. You give it a contract; it figures out the mint call itself and
fires all your wallets the instant the sale opens.

## How the auto-detect works
1. **Verified contract** → reads the ABI, finds the mint function and price.
2. **Unverified contract** → reads the raw bytecode and matches function
   **selectors locally** (no external database, nothing to rate-limit), recovering
   common mints like `mint(uint256)`, `publicMint(uint256)`, `claim(uint256)`.
3. **Simple / public mint** → builds calldata for every wallet automatically.
4. **Proof / signature mint** (Merkle proof or backend signature) → detected and
   named plainly. These need each wallet's own `calldata` from the project — no
   tool can invent that value, because it isn't on-chain. This is the honest wall
   every mint bot hits; the difference here is it tells you instead of erroring.

You can always set exact `calldata` in config (global or per-wallet); that wins
over auto-detect and is the most reliable path for anything unusual.

## Commands
```bash
python snipe.py --detect     # inspect a contract: prints the mint fn + what to do (no keys needed)
python snipe.py --check      # simulate each wallet once (eligibility), no fire
python snipe.py              # watch, fire all wallets the instant it's live
python snipe.py --now        # fire immediately (sale already open)
```
Overrides: `--contract 0x..|opensea-item-link`, `--calldata 0x..`, `--config other.json`.

## config.json fields
- `rpc_url` — Alchemy/QuickNode endpoint (reads + polling)
- `submit_rpc_url` — optional; endpoint the fire tx is sent through (defaults to rpc_url)
- `contract` — address or an OpenSea **item** link (collection links need the address)
- `calldata` — leave **blank** to auto-detect; set it to force an exact call
- `qty` — quantity per wallet (default 1); auto-detect fills mint args with this
- `value_eth` — price per mint; blank/`"0"` uses the on-chain price if found
- `poll_ms` — how often it checks for open (200 = 0.2s; lower is faster, needs a dedicated RPC)
- `gas_limit` — fallback gas used to pre-sign before open
- `start_time`, `lead_seconds`, `window_seconds` — optional scheduling
- `wallets` — each `{ "name", "key" }`, plus optional per-wallet `calldata`, `qty`, `value_eth`

## Efficiency
- When every wallet shares the same call (simple mint), it watches **one** gate and
  fires all wallets together — far less RPC load than polling each separately.
- Everything is pre-signed; it fires raw with a warmed connection and a send retry.
- On Robinhood Chain the earliest tx to the sequencer wins (not gas), so run it on a
  small VPS near the sequencer for real speed. Use a dedicated RPC so fast polling
  isn't rate-limited.

## Setup
```bash
git clone <your-repo> rh-sniper && cd rh-sniper
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
nano config.json     # rpc, contract, your keys (config.json is git-ignored)
python snipe.py --detect     # sanity-check before mint day
```

## Honest limits
- It can only fire a call you can already make. Proof/signature mints need per-wallet
  calldata from the project.
- Anti-bot mints (1 IP = 1 mint, etc.) block multi-wallet minting regardless of speed;
  some projects void sybil wallets.
- Always confirm the correct contract from the official source — fake OpenSea
  collections are common.
