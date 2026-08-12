# rh-mint-bot — Robinhood Chain multi-wallet mint bot (Telegram-controlled)

Self-hosted. Your keys live in `wallets.json` on your own server and never leave
it. Telegram is your remote control. Chain id 4663, gas token ETH.

Includes a bonus `terminal-sniper/` — a Telegram-free CLI version for the
absolute fastest fire (see its own README).

---

## What it does
- Unlimited wallets: `/newwallet`, `/importwallet`
- Mint by contract (auto-detects the mint call): `/source`, `/simall`, `/armall`, `/autoall`
- Scheduled mint at WL start: `/scheduleall`
- Copy-mint another wallet: `/copy`
- Gas override: `/setgas`

Not included: listing/sniping on OpenSea — that needs an OpenSea API key (gated,
from OpenSea's developer platform). Add the key and it can be wired in.

---

## 1) Put it on GitHub
Easiest from a computer browser:
1. Create a **private** repo on github.com (e.g. `rh-mint-bot`).
2. **Extract this zip** and upload the files (drag them into the repo's "Add file
   → Upload files"). Upload the *files*, not the zip.
3. Commit.

`.gitignore` already excludes `.env` and `wallets.json`, so your secrets never
get uploaded — but only if you never create those files before pushing. Keep the
repo private regardless.

## 2) Clone + set up on your VPS
SSH into the VPS, then:
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git tmux
git clone https://github.com/YOUR_USERNAME/rh-mint-bot.git
cd rh-mint-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## 3) Connect your Telegram bot
The "connection" is just your bot token in `.env`. The script logs into Telegram
with it and answers your commands.

1. In Telegram, message **@BotFather** → `/newbot` (or `/mybots` → your existing
   bot → API Token) → copy the token.
2. Message **@userinfobot** → copy your numeric id.
3. Create your config:
```bash
cp .env.example .env
nano .env
```
Fill in:
```
TELEGRAM_BOT_TOKEN=your_bot_token
AUTHORIZED_USER_ID=your_numeric_id
RH_RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
```
Only that numeric id can command the bot.

4. Add your wallets:
```bash
cp wallets.json.example wallets.json
nano wallets.json     # paste your keys, or leave and use /newwallet later
```

## 4) Run it (stays up after you disconnect)
```bash
tmux new -s bot
source venv/bin/activate
python rh_mint_bot.py
```
When it prints `Bot up...`, detach with **Ctrl+B** then **D** — it keeps running.
Reattach anytime: `tmux attach -t bot`. Stop: attach, then **Ctrl+C**.

In Telegram, send `/start`, then `/wallets` to confirm it's connected.

### Optional: auto-restart on reboot (systemd)
```bash
sudo tee /etc/systemd/system/rhbot.service > /dev/null << UNIT
[Unit]
Description=RH mint bot
After=network.target
[Service]
WorkingDirectory=$HOME/rh-mint-bot
ExecStart=$HOME/rh-mint-bot/venv/bin/python $HOME/rh-mint-bot/rh_mint_bot.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now rhbot
```
Logs: `journalctl -u rhbot -f`

---

## Command reference
| Command | Does |
|---|---|
| `/wallets` | List wallets + balances |
| `/newwallet [n]` | Create n new wallets |
| `/importwallet 0xKEY` | Add an existing wallet |
| `/source <link\|0x>` | Load contract, auto-detect mint fn + price |
| `/setqty <n>` / `/setvalue 0.05` | Quantity / price per mint |
| `/setgas <maxGwei> [prio] [limit]` | Gas override (`auto` resets) |
| `/simall` | Simulate every wallet (eligibility, no gas) |
| `/armall` | Pre-sign every wallet |
| `/mintall` | Fire every eligible wallet now |
| `/autoall [ms]` | Each wallet fires when it becomes eligible |
| `/scheduleall <when>` | Auto-mint all at a set time |
| `/copy 0xADDR [sec]` | Copy-mint a target wallet's mints |
| `/status` | Full config |
| `/cancel` | Stop everything |

`<when>`: `2026-07-20T14:00:00Z`, `+90m`, or an offset like `+06:00`.

## Honest notes
- On this FCFS chain, **latency wins, not gas** — run the VPS near the sequencer.
- Copy-mint **follows** (no mempool, so you can't front-run) and only works for
  simple mints.
- Proof/signature whitelist mints need each wallet's own `calldata` — no bot can
  invent it.
- Back up `wallets.json`. Lose it, lose the wallets.
