# Audit findings and status

Audited upstream commit 5189e79 (static review of all code and SKILL.md files, adversarial tests,
outside verification). No malware, exfiltration or hidden access was found. The issues below are
engineering-safety issues. Each fix lands as its own commit with a regression test.

## Fixed (each fix is its own commit; its regression test is in tests/test_regressions.py)
1. Risk check passed NaN/inf amounts and prices: now BLOCKED (fails closed)
2. prices.json looked fresh while holding old prices: per-ticker `fetched_at`, file `as_of` = oldest ticker
3. Share orders sized from stale or untimed prices passed: blocked past `max_price_age_hours` (default 72)
4. `check_ticker` was never enforced: carried onto orders by budget and rebalance, BLOCKED by risk
5. A "yes" with extra instructions ("yes, use margin") was accepted: only a plain yes counts
6. trailing_stop without trail parameters passed: blocked
7. NaN weights crashed sizing with a raw ValueError: clean SizeError
8. HTTP clients identified as the upstream project: neutral User-Agent, guarded by a test

Upstream tests changed: test_repo.py was replaced (it asserted the author's marketing copy and plugin
files); test_risk.py share-order cases now pass fresh price timestamps (the old behaviour was the bug).

## Open (do before any real money)
1. Gates are enforced by Claude following instructions. Move order submission into your own script that
   asks for approval in the terminal, or use IBKR's draft-only connector. Claude should hold no order tool.
2. trader-risk checks local portfolio.json and prices.json, not the real broker account.
3. Paper/live is a label in config.json; derive it from the real broker endpoint instead.
4. News/SEC text reaches Claude unsanitized (prompt injection). Mark it untrusted.
5. Only a ticker-shaped regex is checked: leveraged/inverse ETFs and OTC symbols pass.
6. No daily loss limit, total exposure cap or kill switch.
7. trader-tax is US-federal only (wash sale, short/long term). Not valid for a German tax resident.
8. trader-pelosi marks filings as seen even if reading them failed.
9. Pin the Alpaca MCP server version; check which brokers and US-listed ETFs are available to you.
10. Cut scope (drop pelosi, screener, tax unless redesigned).
