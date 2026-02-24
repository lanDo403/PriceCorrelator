from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from price_correlator.models import EventMarketInfo, EventMetadata

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_HOME_RU_URL = "https://polymarket.com/ru"
POLYMARKET_EVENT_RU_URL_TEMPLATE = "https://polymarket.com/ru/event/{slug}"

_DEFAULT_EVENT_DURATION_SECONDS = 300

_EVENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_BTC_UPDOWN_SLUG_RE = re.compile(r"^btc-updown-(\d+)m-(\d+)$")
_USD_NUMBER_RE = re.compile(r"\$?\s*([0-9][0-9\s,\.\u00A0]*)")
_SPAN_TAG_RE = re.compile(
    r"<span[^>]*class=(['\"])(?P<class>.*?)\1[^>]*>(?P<value>.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_PRICE_TO_BEAT_SELECTOR_TOKENS = {
    "text-heading-2xl",
    "mt-1",
    "tracking-wide",
    "text-text-secondary",
}
_PLAYWRIGHT_FETCH_LOCK = threading.Lock()

_DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PriceCorrelator/0.1",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def _urlopen_without_proxy(request: Request, timeout: int = 10):
    opener = build_opener(ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def parse_event_slug(event_url_or_slug: str) -> str:
    candidate = event_url_or_slug.strip().strip("/")
    if not candidate:
        raise ValueError("event_url_or_slug must not be empty.")

    if _EVENT_SLUG_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[-2] == "event" and _EVENT_SLUG_RE.fullmatch(parts[-1]):
            return parts[-1]

    raise ValueError(
        "Could not extract an event slug. Expected "
        "https://polymarket.com/ru/event/<slug> or <slug>."
    )


def _fetch_homepage_html_sync() -> str:
    request = Request(
        POLYMARKET_HOME_RU_URL,
        method="GET",
        headers={
            "User-Agent": _DEFAULT_HTTP_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": _DEFAULT_HTTP_HEADERS["Accept-Language"],
        },
    )
    with _urlopen_without_proxy(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Polymarket homepage returned status {response.status}.")
        return response.read().decode("utf-8", errors="ignore")


def _fetch_events_sync(slug: str) -> list[dict[str, Any]]:
    query = urlencode({"slug": slug})
    request = Request(
        f"{GAMMA_EVENTS_URL}?{query}",
        method="GET",
        headers=_DEFAULT_HTTP_HEADERS,
    )
    with _urlopen_without_proxy(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Gamma /events returned status {response.status}.")
        payload = json.loads(response.read().decode("utf-8"))
    return [record for record in _coerce_records(payload, preferred_keys=("events", "data")) if isinstance(record, Mapping)]


def _fetch_markets_sync(slug: str) -> list[dict[str, Any]]:
    query = urlencode({"slug": slug})
    request = Request(
        f"{GAMMA_MARKETS_URL}?{query}",
        method="GET",
        headers=_DEFAULT_HTTP_HEADERS,
    )
    with _urlopen_without_proxy(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Gamma /markets returned status {response.status}.")
        payload = json.loads(response.read().decode("utf-8"))
    return [record for record in _coerce_records(payload, preferred_keys=("markets", "data")) if isinstance(record, Mapping)]


def _fetch_recent_markets_sync(limit: int = 500) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "limit": limit,
            "order": "id",
            "ascending": "false",
        }
    )
    request = Request(
        f"{GAMMA_MARKETS_URL}?{query}",
        method="GET",
        headers=_DEFAULT_HTTP_HEADERS,
    )
    with _urlopen_without_proxy(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Gamma /markets recent list returned status {response.status}.")
        payload = json.loads(response.read().decode("utf-8"))
    return [record for record in _coerce_records(payload, preferred_keys=("markets", "data")) if isinstance(record, Mapping)]


def _fetch_event_page_html_playwright_sync(slug: str, timeout_ms: int = 30_000) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "Playwright is unavailable. Install it with "
            "`python -m pip install playwright` and `python -m playwright install chromium`."
        ) from exc

    event_url = POLYMARKET_EVENT_RU_URL_TEMPLATE.format(slug=slug)
    try:
        with _PLAYWRIGHT_FETCH_LOCK:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        locale="en-US",
                        user_agent=_DEFAULT_HTTP_HEADERS["User-Agent"],
                    )
                    try:
                        page = context.new_page()
                        page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        try:
                            page.wait_for_selector(
                                "span.text-heading-2xl.mt-1.tracking-wide.text-text-secondary",
                                timeout=5_000,
                            )
                        except PlaywrightTimeoutError:
                            pass
                        return page.content()
                    finally:
                        context.close()
                finally:
                    browser.close()
    except Exception as exc:
        raise RuntimeError(
            f"Playwright failed to fetch event HTML: slug={slug}, url={event_url}, "
            f"error={type(exc).__name__}: {exc}"
        ) from exc


def _coerce_records(payload: Any, preferred_keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _build_homepage_event_link_regex(timeframe_minutes: int) -> re.Pattern[str]:
    return re.compile(fr"/(?:[a-z]{{2}}/)?event/(btc-updown-{timeframe_minutes}m-\d+)")


def _extract_btc_updown_slugs(html: str, timeframe_minutes: int) -> list[str]:
    regex = _build_homepage_event_link_regex(timeframe_minutes)
    return list(dict.fromkeys(regex.findall(html)))


def _timestamp_from_slug(slug: str) -> int | None:
    match = _BTC_UPDOWN_SLUG_RE.fullmatch(slug)
    if match is None:
        return None
    return int(match.group(2))


def _duration_seconds_from_slug(slug: str) -> int | None:
    match = _BTC_UPDOWN_SLUG_RE.fullmatch(slug)
    if match is None:
        return None
    return int(match.group(1)) * 60


def _extract_btc_updown_slugs_from_markets(
    market_records: Sequence[Mapping[str, Any]],
    timeframe_minutes: int,
) -> list[str]:
    expected_prefix = f"btc-updown-{timeframe_minutes}m-"
    slugs: list[str] = []
    for record in market_records:
        slug = str(record.get("slug") or "")
        if not slug.startswith(expected_prefix):
            continue
        if _timestamp_from_slug(slug) is None:
            continue
        slugs.append(slug)
    return list(dict.fromkeys(slugs))


def _validate_timeframe_minutes(timeframe_minutes: int) -> None:
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be > 0.")


def _build_time_aligned_btc_updown_slugs(
    timeframe_minutes: int,
    now_seconds: int,
    back_steps: int = 4,
    forward_steps: int = 8,
) -> list[str]:
    timeframe_seconds = timeframe_minutes * 60
    next_end_timestamp = ((now_seconds // timeframe_seconds) + 1) * timeframe_seconds
    slugs: list[str] = []
    for step in range(-back_steps, forward_steps + 1):
        end_timestamp = next_end_timestamp + step * timeframe_seconds
        if end_timestamp <= 0:
            continue
        slugs.append(f"btc-updown-{timeframe_minutes}m-{end_timestamp}")
    return slugs


def _filter_candidates_near_now(
    slugs: Sequence[str],
    timeframe_minutes: int,
    now_seconds: int,
) -> list[str]:
    # Keep candidates around "now" to avoid probing irrelevant far-future/far-past slugs.
    max_distance_seconds = max(timeframe_minutes * 60 * 24, 3600)
    filtered: list[str] = []
    for slug in slugs:
        timestamp = _timestamp_from_slug(slug)
        if timestamp is None:
            filtered.append(slug)
            continue
        if abs(timestamp - now_seconds) <= max_distance_seconds:
            filtered.append(slug)
    return filtered


def _to_epoch_seconds(value: Any) -> int | None:
    def _normalize_epoch(raw_epoch: int) -> int:
        # APIs can provide unix time in milliseconds.
        if raw_epoch >= 1_000_000_000_000:
            return raw_epoch // 1000
        return raw_epoch

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _normalize_epoch(int(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return _normalize_epoch(int(raw))
        normalized = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return [str(item) for item in parsed if item is not None and str(item)]
            return []
        return [part for part in (chunk.strip() for chunk in raw.split(",")) if part]
    return []


def _extract_up_down_token_ids(market: Mapping[str, Any]) -> tuple[str, str]:
    outcomes = [item.lower() for item in _normalize_str_list(market.get("outcomes"))]
    token_ids = _normalize_str_list(market.get("clobTokenIds") or market.get("clob_token_ids"))

    outcome_map: dict[str, str] = {}
    if outcomes and token_ids and len(outcomes) == len(token_ids):
        outcome_map = dict(zip(outcomes, token_ids, strict=False))
    elif isinstance(market.get("tokens"), list):
        for token in market["tokens"]:
            if not isinstance(token, Mapping):
                continue
            token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
            outcome = token.get("outcome") or token.get("name")
            if token_id is None or outcome is None:
                continue
            outcome_map[str(outcome).strip().lower()] = str(token_id)

    up_token = outcome_map.get("up") or outcome_map.get("yes", "")
    down_token = outcome_map.get("down") or outcome_map.get("no", "")
    if not up_token and token_ids:
        up_token = token_ids[0]
    if not down_token and len(token_ids) >= 2:
        down_token = token_ids[1]
    return up_token, down_token


def _parse_price_candidate(raw_value: str) -> float | None:
    cleaned = raw_value.strip().replace("\u00A0", "").replace(" ", "").replace("$", "")
    if not cleaned:
        return None

    comma_count = cleaned.count(",")
    dot_count = cleaned.count(".")
    if comma_count > 0 and dot_count > 0:
        cleaned = cleaned.replace(".", "").replace(",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else cleaned.replace(",", "")
    elif comma_count > 0:
        cleaned = cleaned.replace(",", ".") if comma_count == 1 and len(cleaned.split(",")[-1]) <= 3 else cleaned.replace(",", "")
    elif dot_count > 1:
        left, _, right = cleaned.rpartition(".")
        cleaned = left.replace(".", "") + "." + right

    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if 1_000 < value < 1_000_000 else None


def _extract_price_to_beat_from_event_html(html: str) -> float | None:
    normalized = unescape(html)
    for match in _SPAN_TAG_RE.finditer(normalized):
        class_value = str(match.group("class") or "").lower()
        class_tokens = {token.strip() for token in class_value.split() if token.strip()}
        if not _PRICE_TO_BEAT_SELECTOR_TOKENS.issubset(class_tokens):
            continue
        if not any(token.startswith("font-[") for token in class_tokens):
            continue
        text_value = _HTML_TAG_RE.sub("", str(match.group("value") or "")).strip()
        number_match = _USD_NUMBER_RE.search(text_value)
        if number_match is None:
            continue
        price = _parse_price_candidate(number_match.group(1))
        if price is not None:
            return price
    return None


class GammaEventsClient:
    """Polymarket Gamma API client for event and market metadata."""

    def __init__(
        self,
        fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
        market_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
        recent_markets_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
        homepage_fetcher: Callable[[], str] | None = None,
        event_page_fetcher: Callable[[str], str] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._fetcher = fetcher or _fetch_events_sync
        self._market_fetcher = market_fetcher or _fetch_markets_sync
        self._recent_markets_fetcher = recent_markets_fetcher or _fetch_recent_markets_sync
        self._homepage_fetcher = homepage_fetcher or _fetch_homepage_html_sync
        self._event_page_fetcher = event_page_fetcher or _fetch_event_page_html_playwright_sync
        self._log = logger or (lambda _: None)
        self._event_html_warning_once: set[str] = set()

    async def __aenter__(self) -> GammaEventsClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def fetch_event_by_slug(self, slug: str) -> EventMetadata:
        records = await asyncio.to_thread(self._fetcher, slug)
        record = self._select_record_by_slug(records, slug)
        if record is None:
            raise LookupError(f"Event not found in Gamma API: slug={slug}.")
        return EventMetadata(
            slug=str(record.get("slug") or slug),
            question=str(record.get("question") or record.get("title") or slug),
            description=str(record.get("description") or ""),
            resolution_source=str(record.get("resolutionSource") or record.get("resolution_source") or ""),
        )

    async def fetch_event_market_info(self, slug: str, include_price_to_beat: bool = True) -> EventMarketInfo:
        market_records = await asyncio.to_thread(self._market_fetcher, slug)
        market = self._select_record_by_slug(market_records, slug)
        if market is None:
            event_records = await asyncio.to_thread(self._fetcher, slug)
            market = self._select_market_from_events(event_records, slug)
        if market is None:
            raise LookupError(f"Market not found in Gamma API: slug={slug}.")

        start_s = _to_epoch_seconds(
            market.get("startDate")
            or market.get("start_date")
            or market.get("startTimestamp")
            or market.get("start")
        )
        end_s = _to_epoch_seconds(
            market.get("endDate")
            or market.get("end_date")
            or market.get("endTimestamp")
            or market.get("end")
        )
        slug_end = _timestamp_from_slug(slug)
        if end_s is None and slug_end is not None:
            end_s = slug_end
        if start_s is None and end_s is not None:
            duration_seconds = _duration_seconds_from_slug(slug) or _DEFAULT_EVENT_DURATION_SECONDS
            start_s = end_s - duration_seconds
        if start_s is None or end_s is None:
            raise LookupError(f"Could not extract market timer for slug={slug}.")

        up_token_id, down_token_id = _extract_up_down_token_ids(market)
        price_to_beat: float | None = None
        if include_price_to_beat:
            try:
                event_html = await asyncio.to_thread(self._event_page_fetcher, slug)
            except Exception as exc:  # noqa: BLE001
                warning_key = f"{slug}|{type(exc).__name__}|{exc}"
                if warning_key not in self._event_html_warning_once:
                    self._event_html_warning_once.add(warning_key)
                    self._log(
                        f"warning: event_html_fetch_failed: slug={slug}, "
                        f"error={type(exc).__name__}: {exc}"
                    )
            else:
                price_to_beat = _extract_price_to_beat_from_event_html(event_html)

        return EventMarketInfo(
            slug=str(market.get("slug") or slug),
            title=str(market.get("question") or market.get("title") or slug),
            start_timestamp_s=start_s,
            end_timestamp_s=end_s,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            price_to_beat=price_to_beat,
        )

    async def discover_latest_btc_updown_slug(self, timeframe_minutes: int = 5) -> str:
        _validate_timeframe_minutes(timeframe_minutes)
        candidates = await self._discover_btc_updown_candidates(timeframe_minutes=timeframe_minutes)
        if not candidates:
            raise LookupError(
                f"Could not find BTC Up/Down {timeframe_minutes}m events on the Polymarket homepage."
            )
        return candidates[0]

    async def discover_latest_btc_updown_5m_slug(self) -> str:
        return await self.discover_latest_btc_updown_slug(timeframe_minutes=5)

    async def fetch_active_btc_updown_market_info(
        self,
        timeframe_minutes: int = 5,
        now_seconds: int | None = None,
    ) -> EventMarketInfo:
        _validate_timeframe_minutes(timeframe_minutes)
        now = int(time.time()) if now_seconds is None else now_seconds
        discovered_candidates = await self._discover_btc_updown_candidates(
            timeframe_minutes=timeframe_minutes,
            now_seconds=now,
            limit=24,
            enrich_with_recent=True,
        )
        aligned_candidates = _build_time_aligned_btc_updown_slugs(
            timeframe_minutes=timeframe_minutes,
            now_seconds=now,
        )
        candidates = list(dict.fromkeys(aligned_candidates + discovered_candidates))
        near_candidates = _filter_candidates_near_now(
            slugs=candidates,
            timeframe_minutes=timeframe_minutes,
            now_seconds=now,
        )
        if near_candidates:
            candidates = near_candidates
        if not candidates:
            raise LookupError(
                f"Could not find BTC Up/Down {timeframe_minutes}m candidate events on Polymarket."
            )

        markets: list[EventMarketInfo] = []
        for slug in candidates:
            try:
                markets.append(await self.fetch_event_market_info(slug, include_price_to_beat=False))
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"warning: active_market_candidate_failed: slug={slug}, "
                    f"error={type(exc).__name__}: {exc}"
                )
                continue
        if not markets:
            raise LookupError(
                f"Could not load metadata for BTC Up/Down {timeframe_minutes}m candidate events."
            )

        active = [market for market in markets if market.start_timestamp_s <= now < market.end_timestamp_s]
        if active:
            return min(active, key=lambda market: market.end_timestamp_s)
        future = [market for market in markets if market.start_timestamp_s > now]
        if future:
            return min(future, key=lambda market: market.start_timestamp_s)
        return max(markets, key=lambda market: market.end_timestamp_s)

    async def fetch_active_btc_updown_5m_market_info(self, now_seconds: int | None = None) -> EventMarketInfo:
        return await self.fetch_active_btc_updown_market_info(timeframe_minutes=5, now_seconds=now_seconds)

    async def _discover_btc_updown_candidates(
        self,
        timeframe_minutes: int,
        now_seconds: int | None = None,
        limit: int = 8,
        enrich_with_recent: bool = False,
    ) -> list[str]:
        html = await asyncio.to_thread(self._homepage_fetcher)
        unique = set(_extract_btc_updown_slugs(html, timeframe_minutes=timeframe_minutes))
        if enrich_with_recent or not unique:
            try:
                market_records = await asyncio.to_thread(self._recent_markets_fetcher)
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"warning: fallback market discovery failed for {timeframe_minutes}m: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                unique.update(_extract_btc_updown_slugs_from_markets(market_records, timeframe_minutes))

        if now_seconds is None:
            return sorted(unique, key=lambda slug: _timestamp_from_slug(slug) or 0, reverse=True)[:limit]

        # For active-market selection we need candidates around current time, not only max timestamp.
        return sorted(
            unique,
            key=lambda slug: abs((_timestamp_from_slug(slug) or now_seconds) - now_seconds),
        )[:limit]

    @staticmethod
    def _select_record_by_slug(records: Sequence[Mapping[str, Any]], slug: str) -> Mapping[str, Any] | None:
        for record in records:
            if str(record.get("slug") or "") == slug:
                return record
        return None

    @staticmethod
    def _select_market_from_events(event_records: Sequence[Mapping[str, Any]], slug: str) -> Mapping[str, Any] | None:
        for event in event_records:
            markets = event.get("markets")
            if not isinstance(markets, list):
                continue
            for market in markets:
                if isinstance(market, Mapping) and str(market.get("slug") or "") == slug:
                    return market
        return None
