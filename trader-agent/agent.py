#!/usr/bin/env python3
"""A language model as a decision rule, kept on the same leash as every other rule. Standard library only.

The backtester asks a rule one question per bar: hold (1) or stay out (0). This module answers it by asking a
model once per trading day, after the first DECISION_BAR bars of that day, and then holding the answer to the
close (the backtester flattens every day at the close). Everything the model sees is built here, from the
anonymized bars up to that moment and nothing else:

  * no ticker, no dates, no times of day, no file names; prices only as basis-point moves
  * the last LOOKBACK_DAYS full days in compact form, plus today's bars so far
  * the cost of one trade side, so it can decide whether a move is worth taking
  * a fixed instruction and a fixed answer format: one JSON object {"position": 0|1, "reason": "..."}

Every decision (and every failure to give a usable answer, which counts as "stay out") is written to a
hash-chained ledger in `<folder>/agent`, keyed by a hash of the exact prompt. A rerun reads its answers from the
ledger and pays for nothing, so the result can be reproduced and the model cannot be asked again until it
answers differently. A hard budget in US dollars, counted from the token usage the API reports, stops the run
before it can overspend; decisions already paid for stay in the ledger and a rerun continues from there.

The API key is read from the environment variable ANTHROPIC_API_KEY on the computer that runs this. It is never
printed, logged or put in a prompt, and error messages have it removed.

Limits: a model that remembers how the real period went may recognise it despite the hidden names, dates and
rebased prices; a result on past data is therefore practice, and the weeks after the model's knowledge (and
forward paper trading) are the ones that count.
"""

import hashlib
import importlib.util
import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load(os.path.join(ROOT, "trader-training", "train.py"), "trader_train_agent")

PROMPT_VERSION = "2"
DECISION_BAR = 2          # the model decides once the day has this many bars; the order fills one bar later
LOOKBACK_DAYS = 5
MAX_REASON = 500
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Request settings. Every listed model thinks before it answers (adaptive thinking is on by default and cannot be
# switched off on all of them), and thinking tokens count against max_tokens and are billed as output. A small
# max_tokens cuts the answer off before the JSON, so it is set high; the cost is only what is actually written.
# Effort is fixed explicitly because its default differs between models (medium on Haiku 5.5 and Opus 5.5, high
# on Sonnet 5.5 and Fable 5.1), which would make two models' runs differ in more than the model.
# Source: the claude-api reference bundled with Claude Code, read 2026-11-06. No temperature is sent: current
# models reject non-default sampling settings.
MAX_TOKENS = 16000
EFFORT = "medium"

# US dollars per million tokens: (input, output). Only models listed here can be run, so spend can be counted.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
}

INSTRUCTIONS = (
    "You are deciding, once per trading day, whether to hold a long position in one stock until the close of "
    "that day. The data is anonymized: you are not told which stock or which dates, and prices are shown only "
    "as moves in basis points (1 bp = 0.01%). You cannot short. Your order fills at the open of the bar after "
    "the one you last see, every trade side costs the amount stated below, and the position is closed at the end "
    "of the day whatever you answer. Answer 0 (stay in cash) unless you expect today's remaining move to beat the "
    "costs. Reply with one JSON object and nothing else: "
    '{"position": 0 or 1, "reason": "one short sentence"}'
)


class BudgetExceeded(RuntimeError):
    pass


def request_settings():
    return {"max_tokens": MAX_TOKENS, "effort": EFFORT, "api_version": API_VERSION}


def _settings_text():
    return json.dumps(request_settings(), sort_keys=True, separators=(",", ":"))


def instructions_hash():
    """Hash of everything that shapes an answer besides the model and the bars: the instructions and the request
    settings. A lock stores it, so changing any of them after a lock is refused."""
    return hashlib.sha256((PROMPT_VERSION + "\n" + INSTRUCTIONS + "\n" + _settings_text()).encode("utf-8")).hexdigest()


def worst_case_cost(model, prompt):
    """The most one call can cost: every byte of the prompt as a token (a token is at least one byte in practice)
    and an answer that uses all of max_tokens."""
    return cost(model, {"input_tokens": len(prompt.encode("utf-8")), "output_tokens": MAX_TOKENS})


def price_for(model):
    if model not in PRICES:
        raise ValueError("unknown model %r: its spend could not be counted. Known: %s" % (model, sorted(PRICES)))
    return PRICES[model]


def cost(model, usage):
    pin, pout = price_for(model)
    return (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout) / 1e6


def per_side_bps(costs):
    return costs["spread_bps"] / 2.0 + costs["slippage_bps"] + costs["commission_bps"]


# ---------- what the model is shown ----------

def _day_spans(hist, n_days):
    """[(start, end_exclusive)] of today's bars and up to n_days earlier days, newest first."""
    n = len(hist)
    spans, end = [], n
    for _ in range(n_days + 1):
        if end <= 0:
            break
        day = hist[end - 1]["t"][:10]
        start = end - 1
        while start > 0 and hist[start - 1]["t"][:10] == day:
            start -= 1
        spans.append((start, end))
        end = start
    return spans


def _bps(x, ref):
    return (x / ref - 1.0) * 1e4


def prompt_for(hist, side_bps):
    spans = _day_spans(hist, LOOKBACK_DAYS)
    today = spans[0]
    past = list(reversed(spans[1:]))
    lines = [INSTRUCTIONS, "", "Cost of one trade side: %.1f bps." % side_bps, ""]
    for k, (a, b) in enumerate(past):
        ref = hist[a - 1]["c"] if a > 0 else hist[a]["o"]
        first_open, last_close = hist[a]["o"], hist[b - 1]["c"]
        hi = max(hist[j]["h"] for j in range(a, b))
        lo = min(hist[j]["l"] for j in range(a, b))
        steps, prev = [], ref
        for j in range(a, b):
            steps.append("%+.0f" % _bps(hist[j]["c"], prev))
            prev = hist[j]["c"]
        vol = sum(hist[j]["v"] for j in range(a, b)) / (b - a)
        lines.append("Day %d: gap %+.0f bps, open to close %+.0f bps, range %.0f bps, mean volume %.2f; "
                     "bar-to-bar close moves (bps): %s"
                     % (k - len(past), _bps(first_open, ref), _bps(last_close, first_open),
                        _bps(hi, lo), vol, " ".join(steps)))
    a, b = today
    ref = hist[a - 1]["c"] if a > 0 else hist[a]["o"]
    lines += ["", "Today so far (bps against yesterday's close; volume is relative to the usual bar):"]
    for j in range(a, b):
        x = hist[j]
        lines.append("bar %d: open %+.0f high %+.0f low %+.0f close %+.0f volume %.2f"
                     % (j - a + 1, _bps(x["o"], ref), _bps(x["h"], ref), _bps(x["l"], ref), _bps(x["c"], ref),
                        x["v"]))
    lines += ["", 'Answer with {"position": 0 or 1, "reason": "..."} only.']
    return "\n".join(lines)


def parse_answer(text):
    """(position, reason) from the first JSON object in the reply, or ValueError."""
    if not isinstance(text, str):
        raise ValueError("no text in the reply")
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            pos = obj.get("position")
            if isinstance(pos, bool) or not isinstance(pos, int) or pos not in (0, 1):
                raise ValueError("position must be 0 or 1, got %r" % (pos,))
            reason = obj.get("reason", "")
            return pos, str(reason)[:MAX_REASON]
        i = text.find("{", i + 1)
    raise ValueError("no JSON object with a position in the reply")


# ---------- the model behind the key ----------

class AnthropicClient:
    """Callable prompt -> (text, usage, stop_reason). The key is read once from the environment and kept private."""

    def __init__(self, model, max_tokens=None, effort=None, timeout=300):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ValueError("ANTHROPIC_API_KEY is not set on this computer. Set it in your own terminal "
                             "(never paste it into a chat or a file) and run again.")
        price_for(model)
        self.model, self.timeout = model, timeout
        self.max_tokens = MAX_TOKENS if max_tokens is None else max_tokens
        self.effort = EFFORT if effort is None else effort
        self._key = key

    def __repr__(self):
        return "AnthropicClient(model=%r)" % self.model

    def __call__(self, prompt):
        body = json.dumps({"model": self.model, "max_tokens": self.max_tokens,
                           "output_config": {"effort": self.effort},
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, method="POST")
        req.add_header("x-api-key", self._key)
        req.add_header("anthropic-version", API_VERSION)
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(400).decode("utf-8", "replace")
            except Exception:
                pass
            raise ValueError(self._clean("Anthropic API error %s: %s" % (e.code, detail)))
        except (urllib.error.URLError, OSError) as e:
            raise ValueError(self._clean("could not reach the Anthropic API: %s" % e))
        # Thinking blocks are skipped: only the text blocks hold the answer. Thinking is included in output_tokens.
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        return text, {"input_tokens": int(usage.get("input_tokens", 0)),
                      "output_tokens": int(usage.get("output_tokens", 0))}, data.get("stop_reason")

    def _clean(self, s):
        return str(s).replace(self._key, "***")


# ---------- asking, with a cache, a ledger and a hard budget ----------

class Asker:
    """Asks the model and logs every answer to a hash-chained ledger in `ledger_folder`, keyed by a hash of the
    model, the prompt version, the request settings and the exact prompt. A prompt already answered is served from
    the ledger for free. Before EVERY call the budget is checked against the most that call could cost, so the cap
    is never passed; all entries with a cost count toward it, whatever their kind."""

    def __init__(self, ledger_folder, client, model, budget):
        price_for(model)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
            raise ValueError("budget must be a positive number of US dollars")
        self.folder, self.client, self.model, self.budget = ledger_folder, client, model, budget
        T.ledger_verify(ledger_folder)
        self.cache, paid = {}, 0.0
        for e in T.ledger_read(ledger_folder):
            if "key" in e:
                self.cache[e["key"]] = e
            paid += e.get("cost", 0.0)
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "spent": 0.0, "ledger_spent": paid}

    def key(self, prompt):
        return hashlib.sha256((self.model + "\n" + PROMPT_VERSION + "\n" + _settings_text() + "\n" + prompt)
                              .encode("utf-8")).hexdigest()

    def ask(self, prompt, kind, parse, fallback):
        """parse(text) -> dict of answer fields, or ValueError. An answer that is still unusable after one retry
        is logged with `fallback` as its fields and failed=True."""
        key = self.key(prompt)
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]
        worst = worst_case_cost(self.model, prompt)
        spent, fields, reason, failed, stops = 0.0, dict(fallback), "", True, []
        for attempt in range(2):
            if self.stats["ledger_spent"] + spent + worst > self.budget:
                if spent:
                    break              # the first attempt is paid for: log it as a failed answer, then stop
                raise BudgetExceeded("budget of %.4f USD reached (%.4f spent; one more call could cost up to "
                                     "%.4f); rerun with a higher budget to continue from the saved answers"
                                     % (self.budget, self.stats["ledger_spent"], worst))
            res = self.client(prompt)
            text, usage = res[0], res[1]
            stops.append(res[2] if len(res) > 2 else None)
            self.stats["calls"] += 1
            spent += cost(self.model, usage)
            try:
                fields = parse(text)
                reason = str(fields.pop("reason", ""))
                failed = False
                break
            except ValueError as e:
                reason = "unusable answer (stop reason %s): %s" % (stops[-1], e)
        entry = {"kind": kind, "key": key}
        entry.update(fields)
        entry.update({"reason": reason[:MAX_REASON], "failed": failed, "model": self.model,
                      "prompt_version": PROMPT_VERSION, "settings": request_settings(), "stop_reasons": stops,
                      "cost": spent})
        e = T.ledger_append(self.folder, entry)
        self.cache[key] = e
        self.stats["spent"] += spent
        self.stats["ledger_spent"] += spent
        return e


# ---------- the rule ----------

def make_agent(folder, client, model, costs, budget, active_from):
    """A strategy(hist, position) -> 0/1 for the backtester that asks `client` once per day.

    folder       replay folder; decisions live in <folder>/agent/ledger.jsonl
    client       callable prompt -> (text, usage)
    active_from  bar index; no question is asked and no position taken before it
    """
    side = per_side_bps(costs)
    asker = Asker(os.path.join(folder, "agent"), client, model, budget)
    stats = asker.stats
    stats.update({"days": 0, "invested_days": 0})
    state = {"day": None, "pos": 0}

    def decide(prompt):
        return asker.ask(prompt, "decision", lambda text: dict(zip(("position", "reason"), parse_answer(text))),
                         {"position": 0})

    def strategy(hist, position):
        n = len(hist)
        if n == 0 or n - 1 < active_from:
            return 0
        day = hist[n - 1]["t"][:10]
        bars_today = 1
        while bars_today < n and hist[n - 1 - bars_today]["t"][:10] == day:
            bars_today += 1
        if day != state["day"]:
            state["day"], state["pos"] = day, 0
        if bars_today < DECISION_BAR:
            return 0
        if bars_today == DECISION_BAR:
            e = decide(prompt_for(hist, side))
            state["pos"] = e["position"]
            stats["days"] += 1
            stats["failures"] += 1 if e["failed"] else 0
            stats["invested_days"] += e["position"]
        return state["pos"]

    strategy.stats = stats
    return strategy
