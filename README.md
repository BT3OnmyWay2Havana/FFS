# Night Desk

An always-on crypto trading desk made of small bots. They scan the market, look for setups, watch for large trades and check risk. A Claude "chief of staff" reads their notes and only messages you on Telegram when a trade needs a yes or no. There's also a pixel-art trading floor dashboard where you can watch it all happen.

It starts in **paper mode**: simulated money, live prices. Nothing here guarantees profit. Most rule-based trading loses money after fees, so run it on paper for weeks before you think about the live switch.

## What each desk does

| Desk | Job | Data |
|---|---|---|
| Scanner | Ranks every pair on the exchange by 24h move and traded value, keeps a watch list | Exchange tickers |
| Hunter | Looks for two long setups on hourly candles: a volume breakout above the 48h high, or an RSI turn inside an uptrend | Exchange candles |
| Whale | Flags unusually large trades on the exchange (it can't see on-chain wallets) | Exchange trade feed |
| News | Galaxy Score, AltRank and sentiment changes (optional) | LunarCrush API v4 |
| Risk | Only says no or shrinks a size: stop distance, reward/risk, volatility, open positions, daily loss, cooldown, price drift at approval | Your limits |
| Chief (Claude) | Reads every note and picks which setups deserve your decision, with a thesis and the main risk | Claude API |
| You | Approve or reject on Telegram or the dashboard. Approved trades are filled, then watched for stop and target | |

Proposals expire after 30 minutes if you don't answer. If the price has moved more than 1% by the time you approve, the risk desk blocks the trade.

## Costs to expect

- **Claude API**: only called when the hunter has a fresh setup, and capped by `CLAUDE_MAX_CALLS_PER_DAY` (default 48). Claude Opus 5.5 is billed per token ($4 per million input tokens, $20 per million output tokens). One review is a few thousand tokens, so a busy day should be a few dollars at most. Check your usage page in the Anthropic console after the first day.
- **Server**: see Hosting below.
- **LunarCrush** (optional): the free tier only covers market data, and social data needs a paid plan. Check their pricing page for which tier includes the endpoints you want.
- **Exchange data**: public market data from Kraken needs no account or key.

## Setup

### 1. Keys you need

1. **Anthropic API key**: create one at console.anthropic.com under API keys, and add some credit.
2. **Telegram bot**:
   - In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts and copy the token.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number at `"chat":{"id": ... }`. That's your `TELEGRAM_CHAT_ID`. The bot only obeys that chat.
3. **Dashboard token**: any long random string. `openssl rand -hex 24` makes one.

### 2. Try it on your own computer first (optional, no keys needed)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
DEMO=1 DASHBOARD_TOKEN=local-demo-token-123 python -m nightdesk
```

Open http://127.0.0.1:8080 and enter `local-demo-token-123`. Demo mode uses simulated prices and a simple rule instead of Claude, so you can see the whole approve-and-fill flow at no cost.

### 3. Configure

```bash
cp .env.example .env
nano .env        # fill in the required section
```

## Hosting (recommendation)

A small Linux virtual server with Docker is the simplest reliable choice for something that must run 24/7 and keep its data on disk. I suggest **Hetzner Cloud's smallest shared-CPU server** (currently the CX23, previously called the CX22) running Ubuntu. Hetzner changed its prices in 2026, and reported figures range from about €6 to €8 a month plus VAT, so check their current price list before you buy. DigitalOcean and Vultr have similar plans. Railway or Fly.io also work, but they need a persistent volume for the database, and usage-based billing is harder to predict.

On the server:

```bash
# once
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
git clone <this repo> nightdesk && cd nightdesk
cp .env.example .env && nano .env
mkdir -p data && sudo chown 1000:1000 data

# start (and restart automatically after reboots or crashes)
sudo docker compose up -d --build
sudo docker compose logs -f        # watch it start; Ctrl+C to stop watching
```

The dashboard only listens on the server itself. To open it from your computer, use an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 root@<server-ip>
```

Then browse to http://127.0.0.1:8080. Approvals also work entirely through Telegram, so you don't need the dashboard day to day.

To update after pulling new code, run `sudo docker compose up -d --build`. Your data stays in `./data`.

## Telegram commands

- `/status` shows equity, today's P/L and open positions
- `/pause` stops new proposals (open positions are still watched)
- `/resume` starts proposals again

A summary arrives every day at `DAILY_SUMMARY_HOUR_UTC`.

## Going live (read all of this first)

Live mode sends real market orders through the exchange API. It's locked behind three separate switches, and a fourth keeps it in dry run:

1. `TRADING_MODE=live`
2. `LIVE_CONFIRM=I ACCEPT REAL MONEY RISK` (exactly)
3. `EXCHANGE_API_KEY` and `EXCHANGE_API_SECRET`, created with **trade permission only, with withdrawals disabled**
4. `LIVE_DRY_RUN=true` (the default) logs every order it would send without sending it. Set it to `false` only once you've watched dry runs behave correctly.

Every live order is also capped by `LIVE_MAX_ORDER_VALUE` (default 50 in your quote currency).

Known limits of the live path:

- **Stops are handled by the bot, not placed on the exchange.** If the server is down, no stop fires. Keep sizes small.
- The live path has been tested against the ccxt interface, not against a real account. Start with dry run, then the smallest order your exchange allows.
- Fees in live mode come from the exchange's order reply and may be in a different currency, so the P/L shown is approximate. Your exchange statement is the record.
- Make sure the exchange you pick is available to you where you live and that your account allows API trading.

## Project layout

```
nightdesk/
  __main__.py      start-up and schedules
  config.py        settings from .env
  desk.py          the floor: runs the desks, routes notes, handles approvals and exits
  desks/           scanner, hunter, whale, news, risk, chief (Claude)
  brokers/         paper and live order handling
  market.py        exchange data via ccxt, plus the offline simulator
  telegram.py      approval prompts and commands
  store.py         SQLite: squawk feed, proposals, positions, equity
  web/             dashboard server and the pixel trading floor page
tests/             unit tests and an end-to-end paper flow
```
