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

European edition (branch `europe`; sources in docs/EUROPE.md):
9. trader-tax was US federal law: replaced by a German tracker (FIFO, stock-loss pot, fund exemption, allowance)
10. trader-pelosi marked filings as seen even when reading failed: replaced by trader-insiders, which skips and
    reports unreadable rows and marks only rows it read
11. Risk accepted only US ticker shapes and let US-domiciled and leveraged ETFs through: European tickers accepted,
    those ETFs blocked (partial lists; the broker is the authority). This closes part of open item 5.
12. Prices of unknown currency could size an order: prices.json is euro and says so; budget, rebalance, risk and
    tax refuse anything else; ticket.py fill keeps the cost in euro and refuses sells (trader-tax logs those)

Upstream tests changed: test_repo.py was replaced (it asserted the author's marketing copy and plugin
files); test_risk.py share-order cases now pass fresh price timestamps (the old behaviour was the bug).

## Open (do before any real money)
1. Gates are enforced by Claude following instructions. Move order submission into your own script that
   asks for approval in the terminal, or use IBKR's draft-only connector. Claude should hold no order tool.
2. trader-risk checks local portfolio.json and prices.json, not the real broker account.
3. Paper/live is a label in config.json; derive it from the real broker endpoint instead.
4. News/SEC text reaches Claude unsanitized (prompt injection). Mark it untrusted.
5. Ticker check is shape plus short lists: unlisted leveraged/inverse funds, OTC symbols and other US ETFs can pass.
6. No daily loss limit, total exposure cap or kill switch.
7. Exchange rates are typed by you (with a date, max 3 days old); nothing fetches the ECB rate. Your broker's
   conversion rate differs.
8. Broker choice for Germany is open (Interactive Brokers Ireland, Trading 212 demo API, others); pin the MCP
   server version of whichever you use and check which instruments it offers you.
9. The stock-loss rule in German tax (2 BvL 3/21) is undecided in every source read; re-check before relying on it.
   Not modelled: Guenstigerpruefung, derivatives, bonds, crypto, pre-2018 fund holdings.
10. Euronext 2027 holidays are not published yet; London, Swiss, Nordic and Spanish tickers have no market clock.
11. The directors' dealings reader was written without a sample of the real BaFin export; check it on a real
    saved table before trusting a "nothing new" result.
12. Cut scope further if a skill is not used (screener, news, analyst cover US companies only).
