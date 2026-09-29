"""Settings, read once from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

LIVE_CONFIRM_PHRASE = "I ACCEPT REAL MONEY RISK"


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader. Existing environment variables always win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [s.strip().upper() for s in _str(name, default).split(",") if s.strip()]


@dataclass
class Settings:
    # Exchange and market
    exchange_id: str = "kraken"
    quote: str = "USD"
    watchlist: list[str] = field(default_factory=list)
    min_quote_volume: float = 5_000_000
    scan_top_n: int = 8

    # Claude (chief of staff)
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_max_calls_per_day: int = 48

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # LunarCrush (optional, paid plan needed for social data)
    lunarcrush_api_key: str = ""

    # Risk limits
    start_equity: float = 10_000
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 10.0
    max_open_positions: int = 4
    max_daily_loss_pct: float = 3.0
    max_atr_pct: float = 8.0
    min_reward_risk: float = 1.5
    min_order_value: float = 10.0
    max_price_drift_pct: float = 1.0
    symbol_cooldown_min: int = 240
    proposal_ttl_min: int = 30

    # Paper trading costs
    paper_fee_pct: float = 0.40
    paper_slippage_pct: float = 0.05

    # Live trading (locked by default)
    trading_mode: str = "paper"
    live_confirm: str = ""
    live_dry_run: bool = True
    live_max_order_value: float = 50.0
    exchange_api_key: str = ""
    exchange_api_secret: str = ""

    # Schedules (seconds)
    scan_every: int = 300
    whale_every: int = 120
    news_every: int = 1800
    chief_every: int = 900
    monitor_every: int = 30
    daily_summary_hour_utc: int = 7

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_token: str = ""

    # Storage
    db_path: str = "data/nightdesk.sqlite3"

    # Offline demo with simulated prices (no keys needed)
    demo: bool = False

    @property
    def live_enabled(self) -> bool:
        """Live orders need three separate switches, on purpose."""
        return (
            self.trading_mode == "live"
            and self.live_confirm == LIVE_CONFIRM_PHRASE
            and bool(self.exchange_api_key and self.exchange_api_secret)
        )

    def problems(self) -> list[str]:
        """Configuration issues worth refusing to start over."""
        out: list[str] = []
        if self.trading_mode not in ("paper", "live"):
            out.append("TRADING_MODE must be 'paper' or 'live'.")
        if self.trading_mode == "live" and not self.live_enabled:
            out.append(
                "TRADING_MODE=live needs LIVE_CONFIRM set to the exact phrase in "
                "the README and exchange API keys. Refusing to start."
            )
        if not self.dashboard_token or len(self.dashboard_token) < 16:
            out.append("DASHBOARD_TOKEN must be set to a random string of 16+ characters.")
        if not self.demo and not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY is missing.")
        if self.claude_effort not in ("low", "medium", "high", "xhigh", "max"):
            out.append("CLAUDE_EFFORT must be low, medium, high, xhigh or max.")
        return out


def from_env() -> Settings:
    return Settings(
        exchange_id=_str("EXCHANGE_ID", "kraken").lower(),
        quote=_str("QUOTE", "USD").upper(),
        watchlist=_list("WATCHLIST", "BTC,ETH,SOL,XRP,ADA,DOGE,LINK,AVAX,DOT,LTC"),
        min_quote_volume=_float("MIN_QUOTE_VOLUME", 5_000_000),
        scan_top_n=_int("SCAN_TOP_N", 8),
        anthropic_api_key=_str("ANTHROPIC_API_KEY"),
        claude_model=_str("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=_str("CLAUDE_EFFORT", "medium").lower(),
        claude_max_calls_per_day=_int("CLAUDE_MAX_CALLS_PER_DAY", 48),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        lunarcrush_api_key=_str("LUNARCRUSH_API_KEY"),
        start_equity=_float("START_EQUITY", 10_000),
        risk_per_trade_pct=_float("RISK_PER_TRADE_PCT", 1.0),
        max_position_pct=_float("MAX_POSITION_PCT", 10.0),
        max_open_positions=_int("MAX_OPEN_POSITIONS", 4),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 3.0),
        max_atr_pct=_float("MAX_ATR_PCT", 8.0),
        min_reward_risk=_float("MIN_REWARD_RISK", 1.5),
        min_order_value=_float("MIN_ORDER_VALUE", 10.0),
        max_price_drift_pct=_float("MAX_PRICE_DRIFT_PCT", 1.0),
        symbol_cooldown_min=_int("SYMBOL_COOLDOWN_MIN", 240),
        proposal_ttl_min=_int("PROPOSAL_TTL_MIN", 30),
        paper_fee_pct=_float("PAPER_FEE_PCT", 0.40),
        paper_slippage_pct=_float("PAPER_SLIPPAGE_PCT", 0.05),
        trading_mode=_str("TRADING_MODE", "paper").lower(),
        live_confirm=_str("LIVE_CONFIRM"),
        live_dry_run=_bool("LIVE_DRY_RUN", True),
        live_max_order_value=_float("LIVE_MAX_ORDER_VALUE", 50.0),
        exchange_api_key=_str("EXCHANGE_API_KEY"),
        exchange_api_secret=_str("EXCHANGE_API_SECRET"),
        scan_every=_int("SCAN_EVERY_SEC", 300),
        whale_every=_int("WHALE_EVERY_SEC", 120),
        news_every=_int("NEWS_EVERY_SEC", 1800),
        chief_every=_int("CHIEF_EVERY_SEC", 900),
        monitor_every=_int("MONITOR_EVERY_SEC", 30),
        daily_summary_hour_utc=_int("DAILY_SUMMARY_HOUR_UTC", 7),
        dashboard_host=_str("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_int("DASHBOARD_PORT", 8080),
        dashboard_token=_str("DASHBOARD_TOKEN"),
        db_path=_str("DB_PATH", "data/nightdesk.sqlite3"),
        demo=_bool("DEMO", False),
    )
