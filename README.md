# Trading

A hardened, personal derivative of an MIT-licensed set of Claude trading skills (see NOTICE.md).
It reads public data, sizes positions to a budget and checks orders against limits. It does not
place orders by itself, and it is paper-only until the open items in docs/FINDINGS.md are closed.

- Not financial advice. Nothing here is a recommendation. You can lose money.
- Install locally only: copy the `trader-*` folders into your own skills directory. There is no
  plugin manifest on purpose, so nothing can auto-update from anywhere.
- Run the tests before every change: `python3 -m unittest discover -s tests -v`
- Keys never go in chat. SEC requires a name and email in `sec_user_agent` (trader/config.json).
- Status of every audit finding (fixed and open): docs/FINDINGS.md
