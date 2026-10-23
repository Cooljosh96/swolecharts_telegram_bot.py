#!/usr/bin/env python3
"""Binance Spot + Bybit USDT perpetual unusual-volume Telegram alert bot.

Uses public exchange market-data APIs and the Telegram Bot API. No exchange
trading keys are required. The bot never places orders.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_BINANCE_API_BASE = "https://data-api.binance.vision"
BINANCE_API_FALLBACKS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)
BYBIT_API_BASE = "https://api.bybit.com"
TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_STATE_FILE = "data/binance-alert-state.json"
USER_AGENT = "multi-exchange-volume-alert-bot/2.0"
logger = logging.getLogger("multi-exchange-alert-bot")


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def format_usdt(value: float) -> str:
    value = max(0.0, value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def format_price(value: float) -> str:
    if value == 0:
        return "n/a"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_age(milliseconds: int) -> str:
    age_seconds = max(0, int(time.time() - milliseconds / 1000))
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h {(age_seconds % 3600) // 60}m ago"
    return f"{age_seconds // 86400}d ago"


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    poll_interval_seconds: int
    volume_multiplier: float
    minimum_quote_volume_usdt: float
    baseline_alpha: float
    volume_alert_cooldown_seconds: int
    pump_price_change_percent: float
    pump_volume_spike: float
    pump_cooldown_seconds: int
    pump_history_samples: int
    new_listing_window_hours: int
    request_timeout_seconds: int
    binance_api_base_url: str
    state_file: Path
    dry_run: bool
    enable_bybit: bool

    @classmethod
    def from_environment(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        dry_run = env_bool("DRY_RUN", False)
        missing = []
        if not token and not dry_run:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id and not dry_run:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ValueError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Set these in your repository secrets/environment, or use "
                "DRY_RUN=true for a no-send test."
            )
        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 60, 10),
            volume_multiplier=env_float("VOLUME_MULTIPLIER", 3.0, 1.1),
            minimum_quote_volume_usdt=env_float("MIN_QUOTE_VOLUME_USDT", 1_000_000.0, 0),
            baseline_alpha=env_float("BASELINE_ALPHA", 0.2, 0.01),
            volume_alert_cooldown_seconds=env_int("VOLUME_ALERT_COOLDOWN_SECONDS", 21_600, 0),
            pump_price_change_percent=env_float("PUMP_PRICE_CHANGE_PERCENT", 3.0, 0.1),
            pump_volume_spike=env_float("PUMP_VOLUME_SPIKE", 2.0, 1.1),
            pump_cooldown_seconds=env_int("PUMP_COOLDOWN_SECONDS", 1_800, 0),
            pump_history_samples=env_int("PUMP_HISTORY_SAMPLES", 6, 3),
            new_listing_window_hours=env_int("NEW_LISTING_WINDOW_HOURS", 24, 1),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", 20, 5),
            binance_api_base_url=os.getenv(
                "BINANCE_API_BASE_URL", DEFAULT_BINANCE_API_BASE
            ).rstrip("/"),
            state_file=Path(os.getenv("STATE_FILE", DEFAULT_STATE_FILE)),
            dry_run=dry_run,
            enable_bybit=env_bool("ENABLE_BYBIT", True),
        )


class JsonState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "known_symbols": {},
            "volume_baselines": {},
            "last_alerts": {},
            "pump_history": {},
            "initialized_at": None,
            "bybit": {
                "volume_baselines": {},
                "last_alerts": {},
                "pump_history": {},
                "initialized_at": None,
            },
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load state file %s: %s; starting fresh", self.path, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


class HttpClient:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def get_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

    def post_form(self, url: str, values: dict[str, str]) -> Any:
        encoded = "&".join(f"{quote(key)}={quote(value)}" for key, value in values.items())
        request = Request(
            url,
            data=encoded.encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} from Telegram: {body}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram request failed: {exc}") from exc


class BinanceClient:
    def __init__(self, http: HttpClient, primary_base_url: str) -> None:
        self.http = http
        self.base_urls = tuple(dict.fromkeys([primary_base_url, *BINANCE_API_FALLBACKS]))

    def _get_json(self, path: str) -> Any:
        errors = []
        for base_url in self.base_urls:
            try:
                return self.http.get_json(f"{base_url}{path}")
            except RuntimeError as exc:
                errors.append(f"{base_url}: {exc}")
                logger.warning("Binance endpoint failed: %s", exc)
        raise RuntimeError("All Binance endpoints failed. " + " | ".join(errors))

    def usdt_symbols(self) -> dict[str, dict[str, Any]]:
        payload = self._get_json("/api/v3/exchangeInfo")
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise RuntimeError("Binance exchangeInfo returned an unexpected response")
        result = {}
        for item in payload["symbols"]:
            if not isinstance(item, dict):
                continue
            if (
                item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed", True)
            ):
                symbol = item.get("symbol")
                if isinstance(symbol, str):
                    result[symbol] = item
        return result

    def twenty_four_hour_tickers(self) -> dict[str, dict[str, Any]]:
        payload = self._get_json("/api/v3/ticker/24hr")
        if not isinstance(payload, list):
            raise RuntimeError("Binance 24hr ticker returned an unexpected response")
        return {
            item["symbol"]: item
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        }


class BybitClient:
    """Public Bybit linear USDT perpetual market data; no API key required."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _get_result(self, path: str) -> list[dict[str, Any]]:
        payload = self.http.get_json(f"{BYBIT_API_BASE}{path}")
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit returned an unexpected response: {str(payload)[:300]}")
        result = payload.get("result")
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Bybit response did not contain a result list")
        return [row for row in rows if isinstance(row, dict)]

    def usdt_perpetual_tickers(self) -> dict[str, dict[str, Any]]:
        rows = self._get_result("/v5/market/tickers?category=linear")
        result = {}
        for ticker in rows:
            symbol = ticker.get("symbol")
            # Linear category includes USDC and other contracts; keep USDT pairs only.
            if isinstance(symbol, str) and symbol.endswith("USDT"):
                result[symbol] = ticker
        return result


class TelegramClient:
    def __init__(self, http: HttpClient, token: str, chat_id: str, dry_run: bool) -> None:
        self.http = http
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run

    def send_message(self, message: str) -> None:
        if self.dry_run:
            logger.info("DRY RUN Telegram message:\n%s", message.replace("<", "").replace(">", ""))
            return
        payload = self.http.post_form(
            f"{TELEGRAM_API_BASE}/bot{self.token}/sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {payload}")


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def make_chart_links(symbol: str, exchange: str) -> str:
    if exchange == "BYBIT":
        trade_url = f"https://www.bybit.com/trade/usdt/{symbol}"
        tv_symbol = f"BYBIT:{symbol}.P"
    else:
        trade_url = f"https://www.binance.com/en/trade/{symbol}?type=spot"
        tv_symbol = f"BINANCE:{symbol}"
    return (
        f'<a href="{trade_url}">{exchange}</a> · '
        f'<a href="https://www.tradingview.com/chart/?symbol={tv_symbol}">TradingView</a>'
    )


def make_new_listing_alert(symbol: str, listing: dict[str, Any], ticker: dict[str, Any]) -> str:
    price = float(ticker.get("lastPrice") or 0)
    volume = float(ticker.get("quoteVolume") or 0)
    listed = int(listing.get("onboardDate") or 0)
    listed_line = format_age(listed) if listed > 0 else "recently observed"
    return (
        "🆕 <b>New Binance USDT listing</b>\n\n"
        f"<b>{html_escape(symbol)}</b>\n"
        f"Listed: {html_escape(listed_line)}\n"
        f"Price: <code>{format_price(price)} USDT</code>\n"
        f"24h volume: <b>{format_usdt(volume)}</b>\n"
        f"Charts: {make_chart_links(symbol, 'BINANCE')}"
    )


def make_volume_alert(
    exchange: str, symbol: str, price: float, current_volume: float,
    baseline: float, multiplier: float, price_change: float,
) -> str:
    sign = "+" if price_change >= 0 else ""
    return (
        f"📈 <b>Unusual {exchange} volume</b>\n\n"
        f"<b>{html_escape(symbol)}</b>\n"
        f"Market: <b>{'USDT perpetual' if exchange == 'BYBIT' else 'Spot USDT'}</b>\n"
        f"24h turnover/volume: <b>{format_usdt(current_volume)}</b>\n"
        f"Baseline: {format_usdt(baseline)}\n"
        f"Spike: <b>{current_volume / baseline:.1f}×</b> (threshold {multiplier:.1f}×)\n"
        f"24h price change: <b>{sign}{price_change:.2f}%</b>\n"
        f"Price: <code>{format_price(price)} USDT</code>\n"
        f"Charts: {make_chart_links(symbol, exchange)}"
    )


def make_pump_alert(
    exchange: str, symbol: str, price: float, price_change: float,
    volume_ratio: float, window_minutes: float, volume_spike_threshold: float,
    volume_24h: float,
) -> str:
    sign = "+" if price_change >= 0 else ""
    market = "USDT perpetual" if exchange == "BYBIT" else "Spot USDT"
    return (
        f"🚨 <b>{exchange} pump alert</b>\n\n"
        f"<b>{html_escape(symbol)}</b>\n"
        f"Market: <b>{market}</b>\n"
        f"Price: <code>{format_price(price)} USDT</code>\n"
        f"Price change: <b>{sign}{price_change:.2f}%</b> in {window_minutes:.1f}m\n"
        f"Volume-rate spike: <b>{volume_ratio:.1f}×</b> "
        f"(threshold {volume_spike_threshold:.1f}×)\n"
        f"24h turnover/volume: <b>{format_usdt(volume_24h)}</b>\n"
        f"Charts: {make_chart_links(symbol, exchange)}"
    )


class AlertBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = HttpClient(config.request_timeout_seconds)
        self.binance = BinanceClient(self.http, config.binance_api_base_url)
        self.bybit = BybitClient(self.http) if config.enable_bybit else None
        self.telegram = TelegramClient(
            self.http, config.telegram_bot_token, config.telegram_chat_id, config.dry_run
        )
        self.state = JsonState(config.state_file)
        self.stop_requested = False

    def request_stop(self, _signum: int, _frame: Any) -> None:
        logger.info("Shutdown requested; stopping after the current cycle")
        self.stop_requested = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        logger.info(
            "Starting multi-exchange alert bot: poll=%ss, volume multiplier=%.1fx, "
            "minimum 24h volume/turnover=%s, Bybit=%s, state=%s",
            self.config.poll_interval_seconds,
            self.config.volume_multiplier,
            format_usdt(self.config.minimum_quote_volume_usdt),
            self.config.enable_bybit,
            self.config.state_file,
        )
        if self.config.dry_run:
            logger.warning("DRY_RUN=true; Telegram messages will only be logged")
        while not self.stop_requested:
            started = time.monotonic()
            try:
                self.check_once()
            except Exception:
                logger.exception("Check cycle failed; will retry on the next cycle")
            elapsed = time.monotonic() - started
            if not self.stop_requested:
                time.sleep(max(1, self.config.poll_interval_seconds - int(elapsed)))
        self.state.save()
        logger.info("Bot stopped cleanly")

    def check_once(self) -> None:
        now = int(time.time())
        alerts_sent = 0

        # Binance Spot scanner: original logic and state keys are retained.
        listings = self.binance.usdt_symbols()
        binance_tickers = self.binance.twenty_four_hour_tickers()
        known_symbols = self.state.data.setdefault("known_symbols", {})
        baselines = self.state.data.setdefault("volume_baselines", {})
        last_alerts = self.state.data.setdefault("last_alerts", {})
        pump_history = self.state.data.setdefault("pump_history", {})
        first_run = self.state.data.get("initialized_at") is None
        listing_window_ms = self.config.new_listing_window_hours * 60 * 60 * 1000

        for symbol, listing in listings.items():
            ticker = binance_tickers.get(symbol)
            if not ticker:
                continue
            current_volume = float(ticker.get("quoteVolume") or 0)
            if current_volume < 0:
                continue
            onboard_date = int(listing.get("onboardDate") or 0)
            is_recent = onboard_date > 0 and (now * 1000 - onboard_date) <= listing_window_ms
            if symbol not in known_symbols:
                should_alert = not first_run or is_recent
                known_symbols[symbol] = now
                if should_alert:
                    self.send_alert(make_new_listing_alert(symbol, listing, ticker), f"new listing {symbol}")
                    alerts_sent += 1

            previous_baseline = float(baselines.get(symbol, 0) or 0)
            if (
                previous_baseline > 0
                and current_volume >= self.config.minimum_quote_volume_usdt
                and current_volume >= previous_baseline * self.config.volume_multiplier
            ):
                key = f"volume:{symbol}"
                last = int(last_alerts.get(key, 0) or 0)
                if now - last >= self.config.volume_alert_cooldown_seconds:
                    msg = make_volume_alert(
                        "BINANCE", symbol, float(ticker.get("lastPrice") or 0),
                        current_volume, previous_baseline, self.config.volume_multiplier,
                        float(ticker.get("priceChangePercent") or 0),
                    )
                    self.send_alert(msg, f"BINANCE unusual volume {symbol}")
                    last_alerts[key] = now
                    alerts_sent += 1

            if previous_baseline <= 0:
                baselines[symbol] = current_volume
            else:
                a = self.config.baseline_alpha
                baselines[symbol] = a * current_volume + (1 - a) * previous_baseline

            history = pump_history.setdefault(symbol, [])
            history.append([float(now), float(ticker.get("lastPrice") or 0), current_volume])
            if len(history) > self.config.pump_history_samples:
                del history[:-self.config.pump_history_samples]
            alerts_sent += self._check_pump(
                "BINANCE", symbol, ticker, history, last_alerts, now,
                volume_field="quoteVolume",
            )

        self.state.data["initialized_at"] = self.state.data.get("initialized_at") or now

        # Bybit linear category includes USDT perpetual contracts. Keys are namespaced
        # with "BYBIT:" so they cannot collide with the original Binance state.
        if self.bybit is not None:
            try:
                bybit_tickers = self.bybit.usdt_perpetual_tickers()
                bybit_state = self.state.data.setdefault("bybit", {})
                bybit_baselines = bybit_state.setdefault("volume_baselines", {})
                bybit_last_alerts = bybit_state.setdefault("last_alerts", {})
                bybit_history = bybit_state.setdefault("pump_history", {})
                bybit_first_run = bybit_state.get("initialized_at") is None
                for symbol, ticker in bybit_tickers.items():
                    current_turnover = float(ticker.get("turnover24h") or 0)
                    price = float(ticker.get("lastPrice") or 0)
                    price_change = float(ticker.get("price24hPcnt") or 0) * 100
                    if current_turnover < self.config.minimum_quote_volume_usdt or price <= 0:
                        continue

                    previous_baseline = float(bybit_baselines.get(symbol, 0) or 0)
                    if (
                        not bybit_first_run
                        and previous_baseline > 0
                        and current_turnover >= previous_baseline * self.config.volume_multiplier
                    ):
                        key = f"volume:{symbol}"
                        last = int(bybit_last_alerts.get(key, 0) or 0)
                        if now - last >= self.config.volume_alert_cooldown_seconds:
                            msg = make_volume_alert(
                                "BYBIT", symbol, price, current_turnover, previous_baseline,
                                self.config.volume_multiplier, price_change,
                            )
                            self.send_alert(msg, f"BYBIT unusual volume {symbol}")
                            bybit_last_alerts[key] = now
                            alerts_sent += 1

                    if previous_baseline <= 0:
                        bybit_baselines[symbol] = current_turnover
                    else:
                        a = self.config.baseline_alpha
                        bybit_baselines[symbol] = a * current_turnover + (1 - a) * previous_baseline

                    history = bybit_history.setdefault(symbol, [])
                    history.append([float(now), price, current_turnover])
                    if len(history) > self.config.pump_history_samples:
                        del history[:-self.config.pump_history_samples]
                    alerts_sent += self._check_pump(
                        "BYBIT", symbol, ticker, history, bybit_last_alerts, now,
                        volume_field="turnover24h",
                    )
                bybit_state["initialized_at"] = bybit_state.get("initialized_at") or now
                logger.info("Bybit: checked %d USDT perpetual tickers", len(bybit_tickers))
            except Exception as exc:
                # Bybit failures do not stop Binance scanning.
                logger.exception("Bybit scan failed; Binance scan completed: %s", exc)

        self.state.save()
        logger.info(
            "Binance checked %d Spot USDT pairs; %d alert(s); %d known pairs; "
            "%d pump histories",
            len(listings), alerts_sent, len(known_symbols), len(pump_history),
        )

    def _check_pump(
        self, exchange: str, symbol: str, ticker: dict[str, Any],
        history: list[list[float]], last_alerts: dict[str, int], now: int,
        volume_field: str,
    ) -> int:
        if len(history) < self.config.pump_history_samples:
            return 0
        first_timestamp, old_price, _ = history[0]
        _, price, current_volume = history[-1]
        price_change = ((price - old_price) / old_price * 100) if old_price > 0 else 0
        changes = [history[i][2] - history[i - 1][2] for i in range(1, len(history))]
        prior_positive = [change for change in changes[:-1] if change > 0]
        avg_prior = sum(prior_positive) / len(prior_positive) if prior_positive else 0
        recent_change = changes[-1]
        volume_ratio = recent_change / avg_prior if avg_prior > 0 and recent_change > 0 else 0
        key = f"pump:{symbol}"
        last = int(last_alerts.get(key, 0) or 0)
        if (
            price_change >= self.config.pump_price_change_percent
            and volume_ratio >= self.config.pump_volume_spike
            and now - last >= self.config.pump_cooldown_seconds
        ):
            window = max(0.1, (now - first_timestamp) / 60)
            if exchange == "BYBIT":
                volume_24h = float(ticker.get("turnover24h") or 0)
            else:
                volume_24h = float(ticker.get("quoteVolume") or 0)
            msg = make_pump_alert(
                exchange, symbol, price, price_change, volume_ratio, window,
                self.config.pump_volume_spike, volume_24h,
            )
            self.send_alert(msg, f"{exchange} pump {symbol}")
            last_alerts[key] = now
            return 1
        return 0

    def send_alert(self, message: str, description: str) -> None:
        try:
            self.telegram.send_message(message)
            logger.info("Sent %s alert", description)
        except Exception:
            logger.exception("Could not send %s alert", description)


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    configure_logging()
    try:
        config = Config.from_environment()
        AlertBot(config).run()
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
