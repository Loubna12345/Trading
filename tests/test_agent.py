import io
import json
import os
import re
import unittest
from unittest import mock

from _load import load
from test_replay import Env, COSTS, WEEKS_DAYS

replay = load("trader-replay", "replay")
agent = load("trader-agent", "agent")
judge = load("trader-agent", "judge")
engine = load("trader-backtest", "engine")

MODEL = "claude-sonnet-5-5"
USAGE = {"input_tokens": 1000, "output_tokens": 100}      # 0.003 USD on the sonnet price list


class Fake:
    """A stand-in model: answers from a function of the prompt, remembers every prompt it saw."""

    def __init__(self, answer=lambda prompt: '{"position": 1, "reason": "test"}', usage=USAGE):
        self.answer, self.usage, self.prompts = answer, usage, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer(prompt), dict(self.usage)


def run_agent(env, client, first=2, last=4, budget=100.0, folder=None):
    plan, blind, weeks, _ = replay._open(env.dir)
    strat = agent.make_agent(folder or env.dir, client, MODEL, plan["costs"], budget=budget,
                             active_from=weeks[first - 1][0])
    run = engine.backtest(blind[:weeks[last - 1][1]], strat, plan["costs"], capital=replay.CAPITAL, flatten_eod=True)
    return strat, run, blind, weeks


class Prompts(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def test_one_call_per_day_in_the_window_and_none_before_it(self):
        fake = Fake()
        run_agent(self, fake, 2, 4)
        self.assertEqual(len(fake.prompts), 3 * WEEKS_DAYS)

    def test_prompts_show_no_dates_names_or_paths(self):
        fake = Fake()
        run_agent(self, fake, 2, 3)
        for p in fake.prompts:
            self.assertIsNone(re.search(r"(19|20)\d\d-\d\d-\d\d", p))
            self.assertNotIn("SECRET", p)
            self.assertNotIn(self.root, p)

    def test_a_prompt_depends_only_on_bars_up_to_that_moment(self):
        full, short = Fake(), Fake()
        run_agent(self, full, 2, 5)
        run_agent(self, short, 2, 3)
        n = len(short.prompts)
        self.assertEqual(short.prompts, full.prompts[:n])

    def test_the_prompt_states_the_cost_and_the_answer_format(self):
        fake = Fake()
        run_agent(self, fake, 2, 2)
        p = fake.prompts[0]
        self.assertIn("2.0 bps", p)          # spread/2 + slippage per side
        self.assertIn('"position"', p)


class Answers(unittest.TestCase):
    def test_parse_accepts_only_zero_or_one(self):
        self.assertEqual(agent.parse_answer('{"position": 1, "reason": "x"}')[0], 1)
        self.assertEqual(agent.parse_answer('Sure! {"position": 0, "reason": "x"} done')[0], 0)
        for bad in ('{"position": 2, "reason": "x"}', '{"position": true, "reason": "x"}', '{"position": "1"}',
                    '{"reason": "x"}', "no json here", "", '{"position": 1', "[1]"):
            with self.assertRaises(ValueError, msg=bad):
                agent.parse_answer(bad)

    def test_reason_is_cut_to_a_sane_length(self):
        self.assertLessEqual(len(agent.parse_answer('{"position": 1, "reason": "%s"}' % ("x" * 5000))[1]), 500)


class Behaviour(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def test_always_flat_never_trades_and_always_long_trades_every_day(self):
        _, run, _, _ = run_agent(self, Fake(lambda p: '{"position": 0, "reason": "no"}'), 2, 3)
        self.assertEqual(len(run["trades"]), 0)
        d2 = os.path.join(self.root, "r2")
        replay.start(source=self.csv, folder=d2, costs=COSTS, periods_per_year=self.PPY, days_per_week=WEEKS_DAYS,
                     holdout=0.34)
        self.dir = d2
        _, run, _, _ = run_agent(self, Fake(), 2, 3)
        self.assertEqual(len(run["trades"]), 2 * WEEKS_DAYS)

    def test_a_bad_answer_is_retried_once_then_counts_as_flat_and_is_logged(self):
        fake = Fake(lambda p: "I refuse")
        strat, run, _, _ = run_agent(self, fake, 2, 2)
        self.assertEqual(len(fake.prompts), 2 * WEEKS_DAYS)      # one retry per day
        self.assertEqual(strat.stats["failures"], WEEKS_DAYS)
        self.assertEqual(len(run["trades"]), 0)

    def test_second_run_costs_nothing_and_gives_the_same_result(self):
        f1, f2 = Fake(), Fake()
        _, run1, _, _ = run_agent(self, f1, 2, 3)
        _, run2, _, _ = run_agent(self, f2, 2, 3)
        self.assertEqual(len(f2.prompts), 0)
        self.assertEqual(run1["equity"], run2["equity"])

    def test_decisions_are_in_a_tamper_evident_ledger(self):
        run_agent(self, Fake(), 2, 2)
        folder = os.path.join(self.dir, "agent")
        n = replay.T.ledger_verify(folder)
        self.assertEqual(n, WEEKS_DAYS)
        path = replay.T.ledger_path(folder)
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(text.replace('"position":1', '"position":0', 1))
        with self.assertRaises(ValueError):
            run_agent(self, Fake(), 2, 2)

    def test_budget_cap_stops_and_a_rerun_continues_from_what_was_paid_for(self):
        fake = Fake()
        with self.assertRaises(agent.BudgetExceeded):
            run_agent(self, fake, 2, 4, budget=0.0100)          # 0.003 per call: stops after four calls
        done = len(fake.prompts)
        self.assertTrue(3 <= done <= 5, done)
        more = Fake()
        run_agent(self, more, 2, 4, budget=100.0)
        self.assertEqual(len(more.prompts), 3 * WEEKS_DAYS - done)

    def test_unknown_model_is_refused_because_its_spend_could_not_be_counted(self):
        with self.assertRaises(ValueError):
            agent.make_agent(self.dir, Fake(), "claude-mystery-9", COSTS, budget=1.0, active_from=0)

    def test_stats_report_calls_spend_and_cache_hits(self):
        strat, _, _, _ = run_agent(self, Fake(), 2, 2)
        self.assertEqual(strat.stats["calls"], WEEKS_DAYS)
        self.assertAlmostEqual(strat.stats["spent"], 0.003 * WEEKS_DAYS, places=9)


class RealClient(unittest.TestCase):
    def reply(self, text='{"position": 1, "reason": "ok"}'):
        body = json.dumps({"content": [{"type": "text", "text": text}],
                           "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_missing_key_is_refused_with_a_hint_not_to_paste_it(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as cm:
                agent.AnthropicClient(MODEL)
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_request_carries_the_key_in_a_header_only(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-123"}):
            c = agent.AnthropicClient(MODEL, max_tokens=200)
            with mock.patch.object(agent.urllib.request, "urlopen", return_value=self.reply()) as op:
                text, usage = c("hello")
        req = op.call_args[0][0]
        self.assertEqual(req.get_header("X-api-key"), "sk-test-123")
        body = json.loads(req.data)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["max_tokens"], 200)
        self.assertEqual(body["messages"][0]["content"], "hello")
        self.assertNotIn("sk-test-123", req.full_url + req.data.decode())
        self.assertNotIn("sk-test-123", repr(c) + str(c.__dict__.get("model")))
        self.assertEqual(usage["input_tokens"], 10)
        self.assertIn("position", text)

    def test_api_errors_become_refusals_that_never_echo_the_key(self):
        import urllib.error
        err = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key sk-test-123"}'))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-123"}):
            c = agent.AnthropicClient(MODEL)
            with mock.patch.object(agent.urllib.request, "urlopen", side_effect=err):
                with self.assertRaises(ValueError) as cm:
                    c("hi")
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("sk-test-123", str(cm.exception))


class Judge(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def lock(self, window="open", rule=("trend", {"window": 3}), **kw):
        args = dict(folder=self.dir, window=window, rule=rule[0], rule_params=rule[1], model=MODEL, budget=50.0,
                    first_week=2 if window == "open" else None)
        args.update(kw)
        return judge.lock(**args)

    def test_run_needs_a_lock_first(self):
        with self.assertRaises(ValueError):
            judge.run(self.dir, Fake())

    def test_lock_is_in_the_ledger_and_fixes_the_window(self):
        e = self.lock()
        self.assertEqual((e["first_week"], e["last_week"]), (2, 16))
        self.assertEqual(e["window"], "open")
        entries = replay.T.ledger_read(self.dir)
        self.assertEqual(entries[-1]["kind"], "comparison_lock")
        self.assertIn("instructions_hash", e)

    def test_one_lock_per_window(self):
        self.lock()
        with self.assertRaises(ValueError):
            self.lock()

    def test_sealed_window_covers_the_sealed_weeks(self):
        e = self.lock("sealed")
        self.assertEqual((e["first_week"], e["last_week"]), (17, 25))

    def test_open_window_cannot_reach_into_the_sealed_weeks(self):
        with self.assertRaises(ValueError):
            self.lock("open", last_week=20)

    def test_run_scores_agent_rule_flat_and_hold_over_the_same_weeks(self):
        self.lock(last_week=5)
        res = judge.run(self.dir, Fake(lambda p: '{"position": 0, "reason": "no"}'))
        self.assertEqual(res["weeks"], 4)
        self.assertAlmostEqual(res["agent"]["total_return"], 0.0, places=12)
        self.assertEqual(res["flat"]["total_return"], 0.0)
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        a, b = weeks[1][0], weeks[4][1]
        run = engine.backtest(blind[:b], replay.strategies.make("trend", window=3), COSTS, capital=replay.CAPITAL,
                              flatten_eod=True)
        self.assertAlmostEqual(res["rule"]["total_return"], run["equity"][b - 1] / run["equity"][a - 1] - 1.0,
                               places=9)
        self.assertAlmostEqual(res["hold"]["total_return"], blind[b - 1]["c"] / blind[a]["o"] - 1.0, places=9)

    def test_the_agent_is_only_called_inside_the_window(self):
        self.lock(last_week=4)
        fake = Fake()
        judge.run(self.dir, fake)
        self.assertEqual(len(fake.prompts), 3 * WEEKS_DAYS)

    def test_result_is_logged_and_the_ledger_still_verifies(self):
        self.lock(last_week=4)
        judge.run(self.dir, Fake())
        kinds = [e["kind"] for e in replay.T.ledger_read(self.dir)]
        self.assertEqual(kinds[-1], "comparison_result")
        self.assertEqual(replay.T.ledger_verify(self.dir), len(kinds))

    def test_sealed_window_runs_once(self):
        self.lock("sealed")
        judge.run(self.dir, Fake())
        with self.assertRaises(ValueError):
            judge.run(self.dir, Fake())

    def test_open_window_can_be_rerun_from_cache_for_free(self):
        self.lock(last_week=4)
        judge.run(self.dir, Fake())
        again = Fake()
        r = judge.run(self.dir, again)
        self.assertEqual(len(again.prompts), 0)
        self.assertEqual(r["agent_stats"]["calls"], 0)

    def test_every_arm_is_scored_with_the_same_costs(self):
        self.lock(last_week=4)
        res = judge.run(self.dir, Fake())
        self.assertEqual(res["costs"], COSTS)


class Verdict(unittest.TestCase):
    CRIT = {"min_weeks": 20, "alpha": 0.05, "min_total_gap": 0.01}

    def v(self, **over):
        s = {"weeks": 30, "agent_total": 0.05, "rule_total": 0.0, "p_agent_better": 0.01, "p_rule_better": 0.99}
        s.update(over)
        return judge.verdict(s, self.CRIT)[0]

    def test_clear_agent_win(self):
        self.assertEqual(self.v(), "AGENT BETTER")

    def test_clear_rule_win(self):
        self.assertEqual(self.v(agent_total=-0.04, rule_total=0.03, p_agent_better=0.99, p_rule_better=0.02),
                         "RULE BETTER")

    def test_too_few_weeks_is_inconclusive_whatever_the_numbers(self):
        self.assertEqual(self.v(weeks=10), "INCONCLUSIVE")

    def test_a_gap_that_could_be_luck_is_no_clear_difference(self):
        self.assertEqual(self.v(p_agent_better=0.20), "NO CLEAR DIFFERENCE")

    def test_an_agent_that_is_better_but_loses_money_is_not_called_better(self):
        self.assertEqual(self.v(agent_total=-0.01, rule_total=-0.10), "NO CLEAR DIFFERENCE")

    def test_a_tiny_gap_is_not_enough_even_if_significant(self):
        self.assertEqual(self.v(agent_total=0.005, rule_total=0.0), "NO CLEAR DIFFERENCE")


class Bootstrap(unittest.TestCase):
    def test_identical_series_have_no_edge(self):
        a = [0.01, -0.02, 0.0, 0.03] * 8
        p = judge.paired_p_better(a, a, seed=1, resamples=500)
        self.assertGreater(p, 0.5)

    def test_a_constant_advantage_is_found_and_the_result_is_repeatable(self):
        rule = [0.01, -0.02, 0.0, 0.03] * 8
        ag = [x + 0.01 for x in rule]
        p1 = judge.paired_p_better(ag, rule, seed=1, resamples=500)
        p2 = judge.paired_p_better(ag, rule, seed=1, resamples=500)
        self.assertLess(p1, 0.01)
        self.assertEqual(p1, p2)


if __name__ == "__main__":
    unittest.main()
