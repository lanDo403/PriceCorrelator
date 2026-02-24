# Event Client Module

Purpose: load event and market metadata for strategy execution.

Responsibilities:

- parse event slug from URL (`parse_event_slug`);
- fetch event metadata (`fetch_event_by_slug`);
- fetch market metadata (`fetch_event_market_info`):
  - event window (`start/end`);
  - outcome token ids (`up/down`);
  - `price_to_beat`.

`price_to_beat` source:

- Playwright-rendered event HTML (`https://polymarket.com/ru/event/<slug>`);
- parsed from the visible target `span` on the page.

Additional behavior:

- discover current `btc-updown-5m-*` or `btc-updown-15m-*` candidates from homepage;
- if homepage does not expose required timeframe, fallback discovery uses Gamma `/markets` list;
- select active BTC Up/Down market by requested timeframe (`5m` or `15m`) at startup.

Code file:

- `src/price_correlator/event_client.py`
