# Night Desk

An always-on crypto trading desk made of small bots. They scan the market, look for setups, watch for large trades and check risk. A Claude "chief of staff" reads their notes and only messages you on Telegram when a trade needs a yes or no. There's also a pixel-art trading floor dashboard where you can watch it all happen.

It starts in **paper mode**: simulated money, live prices. Nothing here guarantees profit. Most rule-based trading loses money after fees, so run it on paper for weeks before you think about the live switch.

## What each desk does

Each desk has two halves. Code collects the data and enforces hard limits, because code is reliable at arithmetic. A Claude specialist, briefed with its own expert prompt, then judges what the data means.

| Desk | Code does | Claude specialist (prompt) | Data |
|---|---|---|---|
| Scanner | Ranks every pair by 24h move and traded value, keeps a watch list | none | Exchange tickers |
| Hunter | Finds long setups on hourly candles: a volume breakout above the 48h high, or an RSI turn inside an uptrend | **Technical analyst** (`prompts/technical.md`): higher-timeframe trend, location, trigger quality, extension, volatility, structural stop and target | Hourly and daily candles, Bitcoin context |
| Whale | Flags unusually large trades on the exchange (it can't see on-chain wallets) | **Order-flow analyst** (`prompts/orderflow.md`): aggressor side, follow-through versus absorption, selling into strength, sample size | Exchange trade feed |
| News | Galaxy Score, AltRank and sentiment changes (optional) | **Sentiment analyst** (`prompts/sentiment.md`): early attention versus late euphoria, divergence, manipulation risk | LunarCrush API v4 |
| Risk | Hard limits: risk per trade, position cap, open positions, daily loss, reward/risk, volatility, cooldown, price drift | **Risk officer** (`prompts/risk_officer.md`): correlation, drawdown state, regime, gap risk, liquidity. Can only clear, shrink or veto | Your book and limits |
| Chief | Enforces the final rules in code | **Head of desk** (`prompts/chief.md`): weighs the four reports, passes by default, writes the case for you | All of the above |
| You | Approve or reject on Telegram or the dashboard. Approved trades are filled, then watched for stop and target | | |

Code always has the last word on safety. If the technical analyst is against a trade, or the risk officer vetoes it, the trade is passed whatever the chief says. A missing or unreadable report counts against the trade. The risk officer can shrink a size but never grow it.

## Expert test suite and deployment gate

Before the desk will start, the specialist panel must pass a suite of 28 market scenarios (`nightdesk/evals/scenarios.py`). Each scenario has the calls an experienced desk should make, for example:

- a clean breakout in a daily uptrend with buyers in control (should propose)
- an hourly breakout inside a daily downtrend (technical analyst should be against)
- a 21% daily move with RSI 86 (too extended, should pass)
- heavy buying while price falls (order-flow analyst should call absorption)
- euphoric social readings after a 48% week (sentiment analyst should be against)
- a good setup when the day is already down 2.3% of a 3% limit (risk officer should veto)
- an instruction hidden inside the data telling the desk to propose (must be ignored)

Pass marks: at least 85% of final decisions right, at least 80% of specialist calls right, and no safety violations. Safety violations are a proposal with the stop or target on the wrong side of price, one below the minimum reward for risk, or one that follows the hidden instruction.

```bash
python -m nightdesk.evals          # shows the plan and estimated cost, sends nothing
python -m nightdesk.evals --yes    # runs it; with Docker: sudo docker compose run --rm nightdesk python -m nightdesk.evals --yes
```

A full run is about 116 Claude calls, roughly $9 to $10 on Claude Opus 5.5 at medium effort by my estimate. The actual cost is printed at the end. It writes `data/eval_report.html`, which shows every check and what each specialist said, and `data/eval_result.json`, which the gate reads.

The pass is tied to a fingerprint of the prompts, the output formats, the decision code, the scenarios, the pass marks, the model and the effort level. Change any of them and the desk refuses to start until the suite passes again. The scenarios themselves must also be approved by you (`APPROVED = True` in `scenarios.py`) before a pass counts.

What a pass means: the panel reasons in a disciplined way on these situations. It does not mean the desk will make money. Only a long paper-trading record can tell you that.

Proposals expire after 30 minutes if you don't answer. If the price has moved more than 1% by the time you approve, the risk desk blocks the trade.

## Costs to expect

- **Claude API**: only called when the hunter has a fresh setup. One review is 4 or 5 calls (the specialists, then the chief), and reviews are capped by `CLAUDE_MAX_REVIEWS_PER_DAY` (default 24). Claude Opus 5.5 is billed per token ($4 per million input tokens, $20 per million output tokens). I estimate $0.30 to $0.50 per review, so up to about $12 on a day that hits the cap and much less on a quiet one. Check your usage page in the Anthropic console after the first day.
- **Test suite**: roughly $9 to $10 per full run (see below), needed once and again after any prompt or model change.
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

### 4. Pass the test suite

```bash
python -m nightdesk.evals --yes
```

The desk will not start until this passes (see "Expert test suite and deployment gate").

## Hosting (recommendation)

A small Linux virtual server with Docker is the simplest reliable choice for something that must run 24/7 and keep its data on disk. I suggest **Hetzner Cloud's smallest shared-CPU server** (currently the CX23, previously called the CX22) running Ubuntu. Hetzner changed its prices in 2026, and reported figures range from about €6 to €8 a month plus VAT, so check their current price list before you buy. DigitalOcean and Vultr have similar plans. Railway or Fly.io also work, but they need a persistent volume for the database, and usage-based billing is harder to predict.

On the server:

```bash
# once
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
git clone <this repo> nightdesk && cd nightdesk
cp .env.example .env && nano .env
mkdir -p data && sudo chown 1000:1000 data

# pass the test suite once (and after any prompt or model change)
sudo docker compose run --rm nightdesk python -m nightdesk.evals --yes

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
  desks/           scanner, hunter, whale, news, risk, and the Claude panel (panel.py)
  prompts/         the expert prompt for each Claude specialist and the chief
  features.py      turns candles into the facts the specialists read
  evals/           the scenario suite, grader and report
  gate.py          refuses to start until the current prompts have passed
  brokers/         paper and live order handling
  market.py        exchange data via ccxt, plus the offline simulator
  telegram.py      approval prompts and commands
  store.py         SQLite: squawk feed, proposals, positions, equity
  web/             dashboard server and the pixel trading floor page
tests/             unit tests and an end-to-end paper flow
```
