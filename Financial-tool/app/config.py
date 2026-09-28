"""Application settings, loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Alpaca ---------------------------------------------------------
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    # Lock #1 of the live-trading triple lock. Paper unless explicitly flipped.
    alpaca_live: bool = False

    # --- Database -------------------------------------------------------
    database_url: str = f"sqlite:///{DATA_DIR / 'trading.db'}"

    # --- Risk defaults (global ceilings; per-symbol values may be lower) --
    max_open_positions: int = 10
    max_deployed_pct: float = 80.0       # % of equity that may be in the market
    daily_loss_limit_pct: float = 3.0    # trips the kill switch
    default_risk_pct: float = 1.0        # % of a symbol's allocation risked per trade
    cooldown_days_after_stop: int = 3

    # --- Strategy selection ---------------------------------------------
    selector_lookback_years: int = 3
    selector_min_trades: int = 12
    selector_folds: int = 4

    # --- Order proposals -------------------------------------------------
    proposal_ttl_minutes: int = 120

    # --- AI analyst (optional) -------------------------------------------
    # "none" | "ollama" (local, private) | "gemini" (sends data to Google)
    ai_provider: str = "none"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- Notifications (optional) ---------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Lets the Telegram buttons actually approve orders rather than just
    # notify. This is a spend-money button living on your phone: anyone
    # holding the phone (or the bot token) can press it. Off by default.
    telegram_allow_approvals: bool = False
    # When approvals are allowed, should they also work while live trading is
    # armed? Live approvals always require a second confirming tap.
    telegram_allow_live_approvals: bool = False
    # Seconds between getUpdates polls. Long polling, so this is a ceiling on
    # how long a tap waits, not a busy loop.
    telegram_poll_timeout: int = 25

    # --- Instrument logos -------------------------------------------------
    # Logos are fetched by the server once per symbol and cached under
    # data/logos/; the browser only ever talks to this app. Turn this off to
    # work fully offline -- symbols then show a locally drawn monogram tile.
    logos_enabled: bool = True
    # {symbol} is the upper-case ticker, {symbol_lower} the lower-case one.
    logo_url_template: str = (
        "https://financialmodelingprep.com/image-stock/{symbol}.png"
    )

    # --- Scout (idea discovery) -----------------------------------------
    # Which universes the scout may look at, comma separated:
    # stocks | sector_etf | bonds | commodities
    scout_universes: str = "stocks,sector_etf,bonds,commodities"
    scout_max_ideas: int = 12
    # Minimum median daily dollar volume for an idea to be suggested.
    scout_min_dollar_volume: float = 5_000_000.0

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider.lower() not in ("", "none", "off")

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def scout_universe_keys(self) -> list[str]:
        return [u.strip() for u in self.scout_universes.split(",") if u.strip()]


settings = Settings()
