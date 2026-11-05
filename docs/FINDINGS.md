# Audit findings and status

Audited upstream commit 5189e79 (static review of all code and SKILL.md files, adversarial tests,
outside verification). No malware, exfiltration or hidden access was found. The issues below are
engineering-safety issues. Each fix lands as its own commit with a regression test.

## Fix queue (status updated as commits land)
1. Risk check passes NaN/inf amounts and prices (fail open)
2. prices.json looks fresh while holding old prices; no per-ticker timestamps
3. Share orders sized from stale or untimed prices pass the risk check
4. `check_ticker` (unverified ticker guess) is never enforced downstream
5. A "yes" with extra instructions ("yes, use margin") is accepted
6. trailing_stop without trail parameters passes
7. NaN weights crash sizing with a raw ValueError
8. HTTP clients identify as the upstream project

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
