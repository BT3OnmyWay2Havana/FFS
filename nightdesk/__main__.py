"""Start the desk: `python -m nightdesk` (reads .env). `DEMO=1` runs offline with simulated prices."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from . import config
from .brokers.paper import PaperBroker
from .desk import Desk
from .desks.chief import ClaudeChief, RuleChief
from .market import ExchangeMarket, SimMarket
from .store import Store
from .telegram import Telegram
from .web.app import create_app

log = logging.getLogger("nightdesk")


async def every(seconds: int, name: str, fn, desk: Desk, first_delay: float = 0) -> None:
    """Run `fn` forever on a fixed cadence; one failure never stops the loop."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await fn()
        except Exception as e:
            log.exception("%s failed", name)
            desk.store.note(name, f"error · {type(e).__name__}: {e}"[:200], level="warn")
        await asyncio.sleep(seconds)


async def main() -> int:
    config.load_dotenv()
    s = config.from_env()
    problems = s.problems()
    if problems:
        for p in problems:
            print(f"Config problem: {p}", file=sys.stderr)
        return 2

    store = Store(s.db_path)
    market = SimMarket(s.quote) if s.demo else ExchangeMarket(s.exchange_id, s.quote)
    chief = ClaudeChief(s.anthropic_api_key, s.claude_model, s.claude_effort) if s.anthropic_api_key and not s.demo else RuleChief()

    if s.live_enabled:
        from .brokers.live import LiveBroker

        broker = LiveBroker(store, s)
        await broker.refresh_cash()
        log.warning("LIVE MODE %s", "(dry run: no real orders)" if s.live_dry_run else "- REAL ORDERS ENABLED")
    else:
        broker = PaperBroker(store, s)

    tg = Telegram(s.telegram_bot_token, s.telegram_chat_id)
    desk = Desk(s, store, market, chief, broker, tg)
    store.note("PM", f"desk online · {broker.mode}{' (demo prices)' if s.demo else ''} · {s.exchange_id} {s.quote}")
    await tg.send(f"Night Desk online [{broker.mode.upper()}]. Send /status any time.")

    server = uvicorn.Server(uvicorn.Config(create_app(desk, s.dashboard_token), host=s.dashboard_host,
                                           port=s.dashboard_port, log_level="warning"))
    tasks = [
        asyncio.create_task(every(s.scan_every, "SCAN", desk.scan_and_hunt, desk)),
        asyncio.create_task(every(s.whale_every, "WHALE", desk.watch_whales, desk, first_delay=20)),
        asyncio.create_task(every(s.news_every, "NEWS", desk.read_news, desk, first_delay=25)),
        asyncio.create_task(every(s.chief_every, "CHIEF", desk.chief_round, desk, first_delay=min(60, s.chief_every))),
        asyncio.create_task(every(s.monitor_every, "MONITOR", desk.monitor, desk, first_delay=5)),
        asyncio.create_task(every(60, "SUMMARY", desk.daily_summary, desk, first_delay=30)),
        asyncio.create_task(tg.poll(desk.decide, desk.command)),
    ]
    server_task = asyncio.create_task(server.serve())
    log.info("Dashboard on http://%s:%s", s.dashboard_host, s.dashboard_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    server.should_exit = True
    await server_task
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await market.close()
    await tg.close()
    if hasattr(broker, "close"):
        await broker.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
