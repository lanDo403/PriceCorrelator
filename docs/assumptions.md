# Assumptions

This project is production-oriented, but strategy results are valid only under these assumptions:

1. System clock is synchronized (NTP) and UTC timestamps are accurate.
2. Chainlink RTDS stream and Polymarket APIs are reachable and return up-to-date data.
3. Event HTML contains the target `price_to_beat` span with expected CSS tokens.
4. Playwright runtime can start Chromium in the target environment.
5. CLOB best ask is treated as an executable taker price at decision time.
6. Profit model is idealized: no fees, no slippage, no partial fills, no latency edge.
7. The script is analytics/simulation logic; it does not place real orders.

If any assumption is violated, outcomes can degrade to `skip` results or biased PnL.
