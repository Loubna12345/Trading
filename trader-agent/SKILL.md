---
name: trader-agent
description: >-
  The Judge. Runs a language model as a trading rule through the same backtester, costs and anonymized weeks as
  a normal rule, and compares the two from the outside with criteria locked in advance. Use when the user says
  "agent vs rules", "let a model decide the trades", "compare the agent with the rule", or wants a fair test of
  whether a model decides better than code.
argument-hint: "[lock|run|status]"
---

# trader-agent

A model is asked once per trading day (after the day's first two bars) whether to hold until the close. It sees
only anonymized moves in basis points for the last five days and today so far, the cost per trade side, and a
fixed instruction. It places no orders. The judge is code, and its criteria are written to the replay ledger
before anything runs.

```bash
export ANTHROPIC_API_KEY=...        # in your own terminal only; never paste a key into a chat or a file
python3 "${CLAUDE_SKILL_DIR}/judge.py" lock --dir trader/replay/a --window open --rule trend --param window=6 \
    --model claude-haiku-5-5 --budget-usd 2
python3 "${CLAUDE_SKILL_DIR}/judge.py" run --dir trader/replay/a
python3 "${CLAUDE_SKILL_DIR}/judge.py" status --dir trader/replay/a
```

## Rules of the comparison

- One lock per window. The open window can be rerun for free from the saved answers; the sealed window runs
  once and uses up the replay's final test for that folder, so freeze the rule and run the open window first.
  Enforced: the sealed window takes only a rule written down for a week in the replay, not one that already had
  its replay final; and after a sealed comparison the replay refuses a final for that rule.
- Arms: the agent, the named rule, flat (never trade) and the stock itself before costs. Same costs, same
  one-bar delay, same end-of-day flatten.
- Verdict needs at least 20 weeks, a paired bootstrap p below 0.05 on the weekly differences, a total-return gap
  of at least 1 point, and a profit after costs for the winner. Otherwise NO CLEAR DIFFERENCE or INCONCLUSIVE.
- A hard budget in US dollars stops the run; decisions already paid for stay in `<folder>/agent/ledger.jsonl`
  (hash-chained) and a rerun continues from there. Before every call the budget is checked against the most that
  call could cost (whole prompt plus `max_tokens` of output), so the cap is never passed. An unusable answer is
  retried once, then counts as "stay out"; its stop reason (for example `max_tokens` or `refusal`) is logged.
- Request settings: `max_tokens` 16000 and effort `medium`, fixed in `agent.py`. The listed models think before
  answering and thinking counts against `max_tokens`, so a small limit cuts the answer off. Effort is set
  explicitly because the default differs by model. The settings are part of the locked hash and the cache key.
- No refusal fallback to another model is used: the arm must be the model that was locked.
- Do a cheap rehearsal (Haiku) in a separate scratch replay folder, not in the one you will judge.
- A model that remembers how the real period went may recognise it despite the hidden names and dates, so a
  past-data win is practice; the sealed weeks after the model's knowledge and forward paper trading count more.
