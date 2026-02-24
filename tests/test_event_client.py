import pytest

import price_correlator.event_client as event_client_module
from price_correlator.event_client import GammaEventsClient, parse_event_slug


def test_parse_event_slug_from_url() -> None:
    slug = parse_event_slug("https://polymarket.com/ru/event/btc-updown-5m-1771157400")
    assert slug == "btc-updown-5m-1771157400"


def test_parse_event_slug_direct_value() -> None:
    slug = parse_event_slug("btc-updown-5m-1771157400")
    assert slug == "btc-updown-5m-1771157400"


def test_parse_event_slug_raises_for_invalid_url() -> None:
    with pytest.raises(ValueError):
        parse_event_slug("https://polymarket.com/ru/some-other-page")


def test_build_time_aligned_btc_updown_slugs_for_15m() -> None:
    slugs = event_client_module._build_time_aligned_btc_updown_slugs(
        timeframe_minutes=15,
        now_seconds=1771241778,  # 2026-02-16 11:36:18 UTC
        back_steps=1,
        forward_steps=1,
    )
    assert slugs == [
        "btc-updown-15m-1771241400",
        "btc-updown-15m-1771242300",
        "btc-updown-15m-1771243200",
    ]


def test_filter_candidates_near_now_drops_far_future_slug() -> None:
    now_s = 1771241778
    slugs = [
        "btc-updown-15m-1771241400",
        "btc-updown-15m-1771242300",
        "btc-updown-15m-1771320600",
    ]
    filtered = event_client_module._filter_candidates_near_now(
        slugs=slugs,
        timeframe_minutes=15,
        now_seconds=now_s,
    )
    assert filtered == [
        "btc-updown-15m-1771241400",
        "btc-updown-15m-1771242300",
    ]


def test_event_page_fetcher_raises_when_playwright_import_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = __import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        if name == "playwright.sync_api":
            raise ImportError("playwright not installed")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(RuntimeError, match="Playwright is unavailable"):
        event_client_module._fetch_event_page_html_playwright_sync("btc-updown-5m-1771157400")


def test_event_page_fetcher_uses_15m_slug_in_playwright_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    calls: dict[str, str] = {}

    class _FakeTimeoutError(Exception):
        pass

    class _FakePage:
        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            calls["url"] = url
            calls["wait_until"] = wait_until
            calls["timeout"] = str(timeout)

        def wait_for_selector(self, selector: str, timeout: int) -> None:
            calls["selector"] = selector

        def content(self) -> str:
            return "<html><body>ok</body></html>"

    class _FakeContext:
        def new_page(self) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            return None

    class _FakeBrowser:
        def new_context(self, locale: str, user_agent: str) -> _FakeContext:
            calls["locale"] = locale
            calls["user_agent"] = user_agent
            return _FakeContext()

        def close(self) -> None:
            return None

    class _FakeChromium:
        def launch(self, headless: bool) -> _FakeBrowser:
            calls["headless"] = str(headless)
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeSyncPlaywrightContext:
        def __enter__(self) -> _FakePlaywright:
            return _FakePlaywright()

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    fake_sync_api_module = types.ModuleType("playwright.sync_api")
    fake_sync_api_module.TimeoutError = _FakeTimeoutError
    fake_sync_api_module.sync_playwright = lambda: _FakeSyncPlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api_module)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))

    slug = "btc-updown-15m-1771317900"
    html = event_client_module._fetch_event_page_html_playwright_sync(slug)

    assert html == "<html><body>ok</body></html>"
    assert calls["url"].endswith(f"/ru/event/{slug}")
    assert calls["wait_until"] == "domcontentloaded"
    assert calls["headless"] == "True"


@pytest.mark.asyncio
async def test_fetch_event_by_slug_success() -> None:
    def fetcher(slug: str) -> list[dict]:
        assert slug == "btc-updown-5m-1771157400"
        return [
            {
                "slug": slug,
                "question": "Will BTC be above ...?",
                "description": "Some description",
                "resolutionSource": "https://data.chain.link/streams/btc-usd",
            }
        ]

    client = GammaEventsClient(fetcher=fetcher)
    event = await client.fetch_event_by_slug("btc-updown-5m-1771157400")

    assert event.slug == "btc-updown-5m-1771157400"
    assert event.question.startswith("Will BTC")
    assert "chain.link" in event.resolution_source


@pytest.mark.asyncio
async def test_fetch_event_by_slug_not_found() -> None:
    client = GammaEventsClient(fetcher=lambda slug: [])
    with pytest.raises(LookupError):
        await client.fetch_event_by_slug("missing-slug")


@pytest.mark.asyncio
async def test_discover_latest_slug_from_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="/ru/event/btc-updown-5m-1771159500">E1</a>
    <a href="/ru/event/btc-updown-5m-1771159800">E2</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)
    client = GammaEventsClient(fetcher=lambda slug: [])
    slug = await client.discover_latest_btc_updown_5m_slug()
    assert slug == "btc-updown-5m-1771159800"


@pytest.mark.asyncio
async def test_discover_latest_slug_for_15m_from_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="/ru/event/btc-updown-5m-1771159800">BTC 5 Minute Up or Down</a>
    <a href="/ru/event/btc-updown-15m-1771160400">BTC 15 Minute Up or Down</a>
    <a href="/ru/event/btc-updown-15m-1771161300">BTC 15 Minute Up or Down</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)
    client = GammaEventsClient(fetcher=lambda slug: [])
    slug = await client.discover_latest_btc_updown_slug(timeframe_minutes=15)
    assert slug == "btc-updown-15m-1771161300"


@pytest.mark.asyncio
async def test_discover_latest_slug_for_15m_falls_back_to_gamma_markets() -> None:
    client = GammaEventsClient(
        fetcher=lambda slug: [],
        homepage_fetcher=lambda: "<html></html>",
        recent_markets_fetcher=lambda: [
            {"slug": "btc-updown-5m-1771159800"},
            {"slug": "btc-updown-15m-1771160400"},
            {"slug": "btc-updown-15m-1771161300"},
        ],
    )
    slug = await client.discover_latest_btc_updown_slug(timeframe_minutes=15)
    assert slug == "btc-updown-15m-1771161300"


@pytest.mark.asyncio
async def test_discover_latest_slug_raises_when_homepage_has_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: "<html></html>")
    client = GammaEventsClient(fetcher=lambda slug: [], recent_markets_fetcher=lambda: [])
    with pytest.raises(LookupError, match="Could not find BTC Up/Down 5m events"):
        await client.discover_latest_btc_updown_5m_slug()


@pytest.mark.asyncio
async def test_fetch_active_market_info_uses_active_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="/ru/event/btc-updown-5m-1771159500">A</a>
    <a href="/ru/event/btc-updown-5m-1771159800">B</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)

    def market_fetcher(slug: str) -> list[dict]:
        if slug == "btc-updown-5m-1771159500":
            return [
                {
                    "slug": slug,
                    "question": "BTC 5 Minute Up or Down",
                    "startTimestamp": 1771159200,
                    "endTimestamp": 1771159500,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-1", "down-1"],
                }
            ]
        if slug == "btc-updown-5m-1771159800":
            return [
                {
                    "slug": slug,
                    "question": "BTC 5 Minute Up or Down",
                    "startTimestamp": 1771159500,
                    "endTimestamp": 1771159800,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-2", "down-2"],
                }
            ]
        return []

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "",
    )
    market = await client.fetch_active_btc_updown_5m_market_info(now_seconds=1771159300)

    assert market.slug == "btc-updown-5m-1771159500"
    assert market.start_timestamp_s == 1771159200
    assert market.end_timestamp_s == 1771159500
    assert market.up_token_id == "up-1"
    assert market.down_token_id == "down-1"


@pytest.mark.asyncio
async def test_fetch_active_market_info_for_15m_uses_active_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="/ru/event/btc-updown-15m-1771160400">A</a>
    <a href="/ru/event/btc-updown-15m-1771161300">B</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)

    def market_fetcher(slug: str) -> list[dict]:
        if slug == "btc-updown-15m-1771160400":
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771159500,
                    "endTimestamp": 1771160400,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-15-1", "down-15-1"],
                }
            ]
        if slug == "btc-updown-15m-1771161300":
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771160400,
                    "endTimestamp": 1771161300,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-15-2", "down-15-2"],
                }
            ]
        return []

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "",
    )
    market = await client.fetch_active_btc_updown_market_info(timeframe_minutes=15, now_seconds=1771160500)

    assert market.slug == "btc-updown-15m-1771161300"
    assert market.start_timestamp_s == 1771160400
    assert market.end_timestamp_s == 1771161300
    assert market.up_token_id == "up-15-2"
    assert market.down_token_id == "down-15-2"


@pytest.mark.asyncio
async def test_fetch_active_market_info_for_15m_prefers_near_now_candidates() -> None:
    now_s = 1771240800

    def market_fetcher(slug: str) -> list[dict]:
        if slug == "btc-updown-15m-1771320600":
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771319700,
                    "endTimestamp": 1771320600,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-future", "down-future"],
                }
            ]
        if slug == "btc-updown-15m-1771241400":
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771240500,
                    "endTimestamp": 1771241400,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-near", "down-near"],
                }
            ]
        return []

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        homepage_fetcher=lambda: '<a href="/ru/event/btc-updown-15m-1771320600">far future</a>',
        recent_markets_fetcher=lambda: [{"slug": "btc-updown-15m-1771241400"}],
        event_page_fetcher=lambda slug: "",
    )
    market = await client.fetch_active_btc_updown_market_info(timeframe_minutes=15, now_seconds=now_s)

    assert market.slug == "btc-updown-15m-1771241400"
    assert market.up_token_id == "up-near"
    assert market.down_token_id == "down-near"


@pytest.mark.asyncio
async def test_fetch_active_market_info_for_15m_uses_time_aligned_slugs_when_discovery_is_far_future() -> None:
    now_s = 1771241778

    def market_fetcher(slug: str) -> list[dict]:
        if slug == "btc-updown-15m-1771242300":
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771241400,
                    "endTimestamp": 1771242300,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-active", "down-active"],
                }
            ]
        if slug.startswith("btc-updown-15m-177132"):
            return [
                {
                    "slug": slug,
                    "question": "BTC 15 Minute Up or Down",
                    "startTimestamp": 1771320000,
                    "endTimestamp": 1771320900,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-future", "down-future"],
                }
            ]
        return []

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        homepage_fetcher=lambda: '<a href="/ru/event/btc-updown-15m-1771320600">far future</a>',
        recent_markets_fetcher=lambda: [{"slug": "btc-updown-15m-1771326900"}],
        event_page_fetcher=lambda slug: "",
    )
    market = await client.fetch_active_btc_updown_market_info(timeframe_minutes=15, now_seconds=now_s)

    assert market.slug == "btc-updown-15m-1771242300"
    assert market.up_token_id == "up-active"
    assert market.down_token_id == "down-active"


@pytest.mark.asyncio
async def test_fetch_active_market_info_logs_failed_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="/ru/event/btc-updown-5m-1771159500">A</a>
    <a href="/ru/event/btc-updown-5m-1771159800">B</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)

    def market_fetcher(slug: str) -> list[dict]:
        if slug == "btc-updown-5m-1771159800":
            raise RuntimeError("market endpoint timeout")
        if slug == "btc-updown-5m-1771159500":
            return [
                {
                    "slug": slug,
                    "question": "BTC 5 Minute Up or Down",
                    "startTimestamp": 1771159200,
                    "endTimestamp": 1771159500,
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["up-1", "down-1"],
                }
            ]
        return []

    warnings: list[str] = []
    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "",
        logger=warnings.append,
    )
    market = await client.fetch_active_btc_updown_5m_market_info(now_seconds=1771159300)

    assert market.slug == "btc-updown-5m-1771159500"
    assert any("warning: active_market_candidate_failed: slug=btc-updown-5m-1771159800" in line for line in warnings)


@pytest.mark.asyncio
async def test_fetch_active_market_info_does_not_fetch_event_html_for_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <a href="/ru/event/btc-updown-15m-1771160400">A</a>
    """
    monkeypatch.setattr("price_correlator.event_client._fetch_homepage_html_sync", lambda: html)

    def market_fetcher(slug: str) -> list[dict]:
        if slug != "btc-updown-15m-1771160400":
            return []
        return [
            {
                "slug": slug,
                "question": "BTC 15 Minute Up or Down",
                "startTimestamp": 1771159500,
                "endTimestamp": 1771160400,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-15", "down-15"],
            }
        ]

    html_calls = {"count": 0}

    def event_page_fetcher(slug: str) -> str:
        html_calls["count"] += 1
        return "<span>unused</span>"

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=event_page_fetcher,
    )
    market = await client.fetch_active_btc_updown_market_info(timeframe_minutes=15, now_seconds=1771159600)

    assert market.slug == "btc-updown-15m-1771160400"
    assert market.price_to_beat is None
    assert html_calls["count"] == 0


@pytest.mark.asyncio
async def test_fetch_event_market_info_extracts_price_to_beat_from_target_span() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        assert slug == "btc-updown-5m-1771161000"
        return [
            {
                "slug": slug,
                "question": "BTC 5 Minute Up or Down",
                "startTimestamp": 1771160700,
                "endTimestamp": 1771161000,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-z", "down-z"],
            }
        ]

    def page_fetcher(slug: str) -> str:
        assert slug == "btc-updown-5m-1771161000"
        return '<span class="text-heading-2xl mt-1 tracking-wide font-[620] text-text-secondary">$68,267.37</span>'

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=page_fetcher,
    )
    market = await client.fetch_event_market_info("btc-updown-5m-1771161000")

    assert market.price_to_beat == 68_267.37


@pytest.mark.asyncio
async def test_fetch_event_market_info_converts_epoch_milliseconds_to_seconds() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        return [
            {
                "slug": slug,
                "question": "BTC 5 Minute Up or Down",
                "startTimestamp": 1_771_160_700_000,
                "endTimestamp": 1_771_161_000_000,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-z", "down-z"],
            }
        ]

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "",
    )
    market = await client.fetch_event_market_info("btc-updown-5m-1771161000")

    assert market.start_timestamp_s == 1_771_160_700
    assert market.end_timestamp_s == 1_771_161_000


@pytest.mark.asyncio
async def test_fetch_event_market_info_returns_none_when_target_span_absent() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        assert slug == "btc-updown-5m-1771161300"
        return [
            {
                "slug": slug,
                "question": "BTC 5 Minute Up or Down",
                "startTimestamp": 1771161000,
                "endTimestamp": 1771161300,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-l", "down-l"],
            }
        ]

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "<div>no price block</div>",
    )
    market = await client.fetch_event_market_info("btc-updown-5m-1771161300")

    assert market.price_to_beat is None


@pytest.mark.asyncio
async def test_fetch_event_market_info_handles_event_html_fetch_failure() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        assert slug == "btc-updown-5m-1771161600"
        return [
            {
                "slug": slug,
                "question": "BTC 5 Minute Up or Down",
                "startTimestamp": 1771161300,
                "endTimestamp": 1771161600,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-f", "down-f"],
            }
        ]

    def failing_page_fetcher(slug: str) -> str:
        raise RuntimeError("playwright browser not installed")

    warnings: list[str] = []
    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=failing_page_fetcher,
        logger=warnings.append,
    )
    market = await client.fetch_event_market_info("btc-updown-5m-1771161600")

    assert market.price_to_beat is None
    assert len(warnings) == 1
    assert warnings[0].startswith("warning: event_html_fetch_failed: slug=btc-updown-5m-1771161600")


@pytest.mark.asyncio
async def test_fetch_event_market_info_logs_html_fetch_failure_once_per_slug() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        return [
            {
                "slug": slug,
                "question": "BTC 5 Minute Up or Down",
                "startTimestamp": 1771161300,
                "endTimestamp": 1771161600,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-f", "down-f"],
            }
        ]

    def failing_page_fetcher(slug: str) -> str:
        raise RuntimeError("playwright browser not installed")

    warnings: list[str] = []
    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=failing_page_fetcher,
        logger=warnings.append,
    )

    await client.fetch_event_market_info("btc-updown-5m-1771161600")
    await client.fetch_event_market_info("btc-updown-5m-1771161600")

    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_fetch_event_market_info_does_not_fallback_to_other_slug() -> None:
    def market_fetcher(slug: str) -> list[dict]:
        assert slug == "btc-updown-5m-1771159800"
        return [
            {
                "slug": "btc-updown-5m-1771160100",
                "question": "Other market",
                "startTimestamp": 1771159800,
                "endTimestamp": 1771160100,
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-x", "down-x"],
            }
        ]

    client = GammaEventsClient(
        fetcher=lambda slug: [],
        market_fetcher=market_fetcher,
        event_page_fetcher=lambda slug: "",
    )
    with pytest.raises(LookupError):
        await client.fetch_event_market_info("btc-updown-5m-1771159800")
