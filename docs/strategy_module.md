# Strategy Module

Purpose: execute entry logic for Polymarket `BTC Up/Down` event markets (`5m` and `15m`).

Runtime modes:

- Single timeframe mode (`5m` or `15m`).
- Parallel mode: two independent strategy runners (`5m` + `15m`) launched simultaneously by CLI.

Entry rules:

- Entry windows:
  - remaining `30..6s`: threshold `150 USD`
  - remaining `5s`: threshold `50 USD`
  - remaining `4s`: threshold `40 USD`
  - remaining `3/2/1s`: threshold `30 USD`
- Distance to `price_to_beat`:
  - side is selected by sign of (`current_price - price_to_beat`) if absolute distance satisfies active threshold.
- Entry is attempted only when liquidity exists in CLOB (`best ask`).

Finalization:

- `final_price` is taken from Chainlink after configured delay (`--final-price-delay-seconds`).
- Event result:
  - `win`
  - `lose`
  - `skip`

Market selection:

- Timeframe is controlled by `market_timeframe_minutes` (`5` or `15`).
- Active market discovery uses Polymarket/Gamma metadata.
- If market switches, strategy advances to next event slug with matching timeframe.

Code files:

- `src/price_correlator/strategy.py`
- `src/price_correlator/clob_client.py`
