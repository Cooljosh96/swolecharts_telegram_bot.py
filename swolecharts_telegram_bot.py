#!/usr/bin/env python3
"""SwoleCharts market-data alerts delivered to a separate Telegram bot.

Uses the public SwoleCharts JSON API and Python standard library only.
Adds volume-spike deduplication, a per-run alert cap, and best-effort volume
amounts when the API response provides compatible numeric fields.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

API = "https://swolecharts.com/api"
TELEGRAM = "https://api.telegram.org"
log = logging.getLogger("swolecharts-alerts")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else default
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first_number(item: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name not in item or item[name] is None or isinstance(item[name], bool):
            continue
        try:
            return float(item[name])
        except (TypeError, ValueError):
            continue
    return None


def esc(value: Any) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def usd(value: Any) -> str:
    n = number(value)
    if abs(n) >= 1e9:
        return f"${n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"${n / 1e6:.2f}M"
    if abs(n) >= 1e3:
        return f"${n / 1e3:.1f}K"
    return f"${n:,.2f}"


def pct_fraction(value: Any) -> str:
    return f"{number(value) * 100:+.2f}%"


def chart_links(symbol: str, venue: str) -> str:
    safe = quote(symbol, safe="")
    if venue.lower() == "bybit":
        venue_url = f"https://www.bybit.com/trade/usdt/{safe}"
        tv_exchange = "BYBIT"
    else:
        venue_url = f"https://www.binance.com/en/futures/{safe}"
        tv_exchange = "BINANCE"
    return (
        f'<a href="{venue_url}">{esc(venue.title())}</a> · '
        f'<a href="https://www.tradingview.com/symbols/{safe}/?exchange={tv_exchange}">TradingView</a> · '
        '<a href="https://swolecharts.com/">SwoleCharts</a>'
    )


def volume_summary(item: dict[str, Any]) -> list[str]:
    """Format volume figures only if compatible fields are present in the API."""
    added = first_number(item, (
        "volume_added_usd", "volumeAddedUsd", "vol_added_usd",
        "volume_added", "volumeAdded", "added_volume", "addedVolume",
    ))
    current = first_number(item, (
        "current_volume_usd", "currentVolumeUsd", "volume_usd",
        "volumeUsd", "quote_volume", "quoteVolume", "current_volume",
        "currentVolume", "volume", "vol_usd", "volUsd",
    ))
    previous = first_number(item, (
        "previous_volume_usd", "previousVolumeUsd", "prev_volume_usd",
        "prevVolumeUsd", "previous_volume", "previousVolume",
        "prev_volume", "prevVolume", "baseline_volume", "baselineVolume",
        "average_volume", "averageVolume", "avg_volume", "avgVolume",
    ))

    if added is not None:
        lines = [f"Volume added: <b>{usd(added)}</b>"]
        if current is not None:
            lines.append(f"Current volume: <b>{usd(current)}</b>")
        if previous is not None:
            lines.append(f"Previous volume: <b>{usd(previous)}</b>")
        if current is not None and previous is not None and previous > 0:
            lines.append(f"Volume increase: <b>{(added / previous) * 100:+.2f}%</b>")
        return lines

    if current is not None and previous is not None:
        delta = current - previous
        lines = [
            f"Previous volume: <b>{usd(previous)}</b>",
            f"Current volume: <b>{usd(current)}</b>",
            f"Volume added: <b>{usd(delta)}</b>",
        ]
        if previous > 0:
            lines.append(f"Volume increase: <b>{(delta / previous) * 100:+.2f}%</b>")
        return lines

    # A signal score is not a volume amount; don't fabricate dollar values.
    return ["Volume added: <i>not supplied in this API alert</i>"]


class Http:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def get_json(self, url: str) -> Any:
        req = Request(url, headers={
            "User-Agent": "swolecharts-telegram-alerts/1.1",
            "Accept": "application/json",
        })
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:250]
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

    def post_form(self, url: str, values: dict[str, str]) -> Any:
        req = Request(
            url, data=urlencode(values).encode(),
            headers={
                "User-Agent": "swolecharts-telegram-alerts/1.1",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:250]
            raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram request failed: {exc}") from exc


class SwoleTelegramBot:
    def __init__(self) -> None:
        self.token = os.getenv("SWOLE_TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("SWOLE_TELEGRAM_CHAT_ID", "").strip()
        self.dry_run = env_bool("DRY_RUN", False)
        if not self.dry_run and (not self.token or not self.chat_id):
            raise ValueError(
                "Set SWOLE_TELEGRAM_BOT_TOKEN and SWOLE_TELEGRAM_CHAT_ID, "
                "or set DRY_RUN=true for testing."
            )

        self.poll = env_int("SWOLE_POLL_SECONDS", 120, 30)
        self.run_seconds = env_int("SWOLE_RUN_SECONDS", 150, 30)
        self.timeout = env_int("REQUEST_TIMEOUT_SECONDS", 20, 5)
        self.max_spike_coins = env_int("MAX_VOLUME_SPIKE_COINS", 20, 1)
        self.spike_scan_seconds = env_int("VOLUME_SPIKE_SCAN_SECONDS", 180, 60)
        self.metric_cooldown = env_int("METRIC_ALERT_COOLDOWN_SECONDS", 1800, 0)
        self.spike_cooldown = env_int("VOLUME_SPIKE_COOLDOWN_SECONDS", 1800, 0)
        self.max_spike_alerts_per_run = env_int("MAX_SPIKE_ALERTS_PER_RUN", 5, 1)

        self.ls_threshold = env_float("LONG_SHORT_CHANGE_THRESHOLD", 0.02)
        self.funding_threshold = env_float("FUNDING_CHANGE_THRESHOLD", 0.00005)
        self.oi_threshold = env_float("OI_CHANGE_THRESHOLD", 0.05)
        self.http = Http(self.timeout)
        self.state_path = Path(os.getenv(
            "SWOLE_STATE_FILE", "data/swolecharts-alert-state.json"
        ))
        self.state: dict[str, Any] = {
            "initialized": False,
            "seen_spikes": [],
            "seen_listings": [],
            "funding": {},
            "last_alerts": {},
            "spike_last_sent": {},
        }
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.state.update(saved)
            except (OSError, json.JSONDecodeError):
                log.warning("Could not read state file; starting with fresh state")
        self.stop = False
        self.last_spike_scan = 0.0
        self.spike_alerts_sent_this_run = 0
        self.logged_volume_schema = False

    def api(self, path: str) -> Any:
        return self.http.get_json(f"{API}{path}")

    def send(self, text: str) -> None:
        if self.dry_run:
            log.info("DRY RUN message:\n%s", text)
            return
        result = self.http.post_form(
            f"{TELEGRAM}/bot{self.token}/sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Telegram rejected message: {result}")

    def alert(self, key: str, text: str, cooldown: int | None = None) -> bool:
        now = int(time.time())
        last_alerts = self.state.setdefault("last_alerts", {})
        previous = int(last_alerts.get(key, 0) or 0)
        wait = self.metric_cooldown if cooldown is None else cooldown
        if now - previous < wait:
            return False
        try:
            self.send(text)
            last_alerts[key] = now
            log.info("Sent alert %s", key)
            return True
        except Exception:
            log.exception("Failed sending alert %s", key)
            return False

    def check_listings(self, first_run: bool) -> None:
        payload = self.api("/new-listings?window=30d")
        listings = payload.get("listings", []) if isinstance(payload, dict) else []
        if not isinstance(listings, list):
            return
        seen = set(self.state.setdefault("seen_listings", []))
        for item in listings:
            if not isinstance(item, dict):
                continue
            venue = str(item.get("venue", "")).lower()
            if venue not in {"binance", "bybit"}:
                continue
            symbol = str(item.get("symbol", ""))
            listed_ms = int(number(item.get("listed_ms")))
            key = f"{venue}:{symbol}:{listed_ms}"
            if not symbol or key in seen:
                continue
            if first_run:
                seen.add(key)
                continue
            message = (
                f"🆕 <b>New {venue.title()} perpetual listing</b>\n\n"
                f"<b>{esc(symbol)}</b>\nAge: {number(item.get('age_days')):.1f} days\n"
                f"Price: <code>{number(item.get('price')):.8g} USDT</code>\n"
                f"24h volume: <b>{usd(item.get('vol_24h'))}</b>\n"
                f"Open interest: <b>{usd(item.get('oi_usd'))}</b>\n"
                f"Funding: <b>{number(item.get('funding')) * 100:.4f}%</b>\n"
                f"Charts: {chart_links(symbol, venue)}"
            )
            if self.alert(f"listing:{key}", message, cooldown=0):
                seen.add(key)
        self.state["seen_listings"] = list(seen)[-3000:]

    def check_metrics(self, first_run: bool) -> None:
        # Retained for compatibility; focused mode below doesn't call this.
        markets_payload = self.api("/markets")
        markets = markets_payload.get("coins", []) if isinstance(markets_payload, dict) else []
        long_short_payload = self.api("/long-short")
        ratios = long_short_payload.get("coins", []) if isinstance(long_short_payload, dict) else []
        funding_state = self.state.setdefault("funding", {})

        for coin in markets:
            if not isinstance(coin, dict):
                continue
            for market in coin.get("perps", []) or []:
                if not isinstance(market, dict):
                    continue
                venue = str(market.get("venue", "")).lower()
                if venue not in {"binance", "bybit"}:
                    continue
                symbol = str(market.get("symbol") or f"{coin.get('base', '')}USDT")
                key = f"{venue}:{symbol}"
                funding = market.get("funding")
                if funding is not None:
                    current = number(funding)
                    old = funding_state.get(key)
                    if old is not None and not first_run and abs(current - number(old)) >= self.funding_threshold:
                        delta = current - number(old)
                        message = (
                            f"💸 <b>Funding-rate change — {venue.title()}</b>\n\n"
                            f"<b>{esc(symbol)}</b>\nPrevious: <b>{number(old) * 100:.4f}%</b>\n"
                            f"Current: <b>{current * 100:.4f}%</b>\n"
                            f"Change: <b>{delta * 100:+.4f} percentage points</b>\n"
                            f"Charts: {chart_links(symbol, venue)}"
                        )
                        self.alert(f"funding:{key}", message)
                    funding_state[key] = current

                oi_change = market.get("oi_1h")
                if oi_change is not None and not first_run and abs(number(oi_change)) >= self.oi_threshold:
                    change = number(oi_change)
                    direction = "increased" if change > 0 else "decreased"
                    message = (
                        f"📊 <b>Open-interest change — {venue.title()}</b>\n\n"
                        f"<b>{esc(symbol)}</b>\n1h change: <b>{pct_fraction(change)}</b> ({direction})\n"
                        f"Open interest: <b>{usd(market.get('oi_usd'))}</b>\n"
                        f"Charts: {chart_links(symbol, venue)}"
                    )
                    hour = int(time.time()) // 3600
                    self.alert(f"oi:{key}:{hour}", message, cooldown=3600)

        for coin in ratios:
            if not isinstance(coin, dict):
                continue
            base = str(coin.get("base", ""))
            for venue in ("binance", "bybit"):
                ratio = coin.get(f"{venue}_accounts")
                if not isinstance(ratio, dict):
                    continue
                symbol = str(ratio.get("symbol") or f"{base}USDT")
                change = number(ratio.get("chg_1h"))
                if first_run or abs(change) < self.ls_threshold:
                    continue
                long_share = number(ratio.get("long"))
                direction = "increased" if change > 0 else "decreased"
                message = (
                    f"👥 <b>Long/short ratio change — {venue.title()}</b>\n\n"
                    f"<b>{esc(symbol)}</b>\nLong account share: <b>{long_share * 100:.1f}%</b>\n"
                    f"1h change: <b>{change * 100:+.2f} percentage points</b> ({direction})\n"
                    f"Charts: {chart_links(symbol, venue)}"
                )
                hour = int(time.time()) // 3600
                self.alert(f"longshort:{venue}:{symbol}:{hour}", message, cooldown=3600)

    def check_volume_spikes(self, first_run: bool) -> None:
        payload = self.api("/markets")
        coins = payload.get("coins", []) if isinstance(payload, dict) else []
        ranked = []
        for coin in coins:
            if isinstance(coin, dict) and coin.get("base"):
                ranked.append((number(coin.get("perp_vol24h")), str(coin["base"]).upper()))
        ranked.sort(reverse=True)

        seen = set(self.state.setdefault("seen_spikes", []))
        spike_last_sent = self.state.setdefault("spike_last_sent", {})

        for _, base in ranked[:self.max_spike_coins]:
            if self.spike_alerts_sent_this_run >= self.max_spike_alerts_per_run:
                log.info(
                    "Reached MAX_SPIKE_ALERTS_PER_RUN=%s; skipping remaining volume alerts this run",
                    self.max_spike_alerts_per_run,
                )
                break
            try:
                coin_data = self.api(f"/coin/{quote(base, safe='')}")
            except Exception as exc:
                log.warning("Skipping volume spikes for %s: %s", base, exc)
                continue

            alerts = coin_data.get("alerts", []) if isinstance(coin_data, dict) else []
            if not isinstance(alerts, list):
                continue

            for item in alerts:
                if not isinstance(item, dict) or str(item.get("kind", "")).lower() != "volume":
                    continue

                # Log one sample per run to identify actual API volume fields.
                if not self.logged_volume_schema:
                    log.info(
                        "SwoleCharts volume alert payload sample: %s",
                        json.dumps(item, ensure_ascii=False),
                    )
                    self.logged_volume_schema = True

                venue = str(item.get("venue", "")).lower()
                if venue not in {"binance", "bybit"}:
                    continue
                symbol = str(item.get("symbol") or f"{base}USDT")
                direction = str(item.get("dir", "unknown")).strip().lower()
                spike_id = item.get("id")

                # Exact event ID prevents the exact same API event from repeating.
                event_key = (
                    f"{venue}:{spike_id}"
                    if spike_id is not None
                    else f"{venue}:{symbol}:{direction}:{item.get('ts', '')}"
                )
                if event_key in seen:
                    continue

                # First run seeds existing events without flooding the chat.
                if first_run:
                    seen.add(event_key)
                    continue

                # Stable cooldown key suppresses fresh IDs for the same symbol/direction.
                cooldown_key = f"{venue}:{symbol}:{direction}"
                now = int(time.time())
                last_sent = int(spike_last_sent.get(cooldown_key, 0) or 0)
                if self.spike_cooldown > 0 and now - last_sent < self.spike_cooldown:
                    seen.add(event_key)
                    continue

                ts = int(number(item.get("ts")))
                age_minutes = max(0, int(time.time() - ts / 1000) // 60) if ts else 0
                volume_lines = volume_summary(item)
                message = (
                    f"⚡ <b>SwoleCharts volume spike — {venue.title()}</b>\n\n"
                    f"<b>{esc(symbol)}</b>\n"
                    f"Direction: <b>{esc(direction.title())}</b>\n"
                    f"Signal strength: <b>{number(item.get('sig')):.2f}</b>\n"
                    + "\n".join(volume_lines)
                    + f"\nDetected: {age_minutes}m ago\n"
                    f"Charts: {chart_links(symbol, venue)}"
                )

                if self.alert(f"spike:{event_key}", message, cooldown=0):
                    seen.add(event_key)
                    spike_last_sent[cooldown_key] = now
                    self.spike_alerts_sent_this_run += 1

        self.state["seen_spikes"] = list(seen)[-5000:]
        if len(spike_last_sent) > 3000:
            self.state["spike_last_sent"] = dict(list(spike_last_sent.items())[-3000:])

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.state_path)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop_handler)
        signal.signal(signal.SIGINT, self.stop_handler)
        started = time.monotonic()
        deadline = started + self.run_seconds
        log.info(
            "SwoleCharts alerts started; polling every %ss for at most %ss",
            self.poll, self.run_seconds,
        )
        if self.dry_run:
            log.warning("DRY_RUN=true; messages will be logged only")

        while not self.stop and time.monotonic() < deadline:
            start = time.monotonic()
            first_run = not bool(self.state.get("initialized"))
            try:
                # Focused mode: listings and volume-spike alerts only.
                self.check_listings(first_run)
                if time.time() - self.last_spike_scan >= self.spike_scan_seconds:
                    self.check_volume_spikes(first_run)
                    self.last_spike_scan = time.time()
                self.state["initialized"] = True
                self.save()
            except Exception:
                log.exception("Polling cycle failed; retrying next cycle")

            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                break
            sleep_for = min(
                remaining,
                max(1, self.poll - int(time.monotonic() - start)),
            )
            for _ in range(sleep_for):
                if self.stop or time.monotonic() >= deadline:
                    break
                time.sleep(1)

        self.save()
        log.info("Bot session finished; saved alert state")

    def stop_handler(self, _signum: int, _frame: Any) -> None:
        self.stop = True


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    SwoleTelegramBot().run()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
