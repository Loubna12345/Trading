import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from unittest import mock

from _load import load
from test_replay import Env, COSTS, WEEKS_DAYS

replay = load("trader-replay", "replay")
agent = load("trader-agent", "agent")
weekly = load("trader-agent", "weekly")

MODEL = "claude-haiku-5-5"
USAGE = {"input_tokens": 2000, "output_tokens": 300}


class Fake:
    """Stand-in model. Weekly prompts get a forecast with a notebook naming the week; daily prompts get a choice."""

    def __init__(self, day=lambda p: 1, week_text=None):
        self.day, self.week_text, self.calls = day, week_text, []

    def __call__(self, prompt):
        if prompt.startswith((weekly.WEEK_INSTRUCTIONS, weekly.WEEK_INSTRUCTIONS_OVERNIGHT)):
            w = int(re.search(r"Now write your forecast for week (\d+)", prompt).group(1))
            self.calls.append(("week", w, prompt))
            text = self.week_text(w) if self.week_text else json.dumps(
                {"expect_return": 0.001, "expect_market": "up", "plan": "plan %d" % w, "notebook": "NB-%d" % w})
            return text, dict(USAGE), "end_turn"
        assert prompt.startswith((weekly.DAY_INSTRUCTIONS, weekly.DAY_INSTRUCTIONS_OVERNIGHT)), prompt[:80]
        self.calls.append(("day", None, prompt))
        return json.dumps({"position": self.day(prompt), "reason": "because"}), dict(USAGE), "end_turn"


class Base(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def lock(self, **kw):
        args = dict(folder=self.dir, model=MODEL, budget=50.0, first_week=2, last_week=4)
        args.update(kw)
        return weekly.lock(**args)

    def entries(self, kind):
        return [e for e in replay.T.ledger_read(self.dir) if e["kind"] == kind]


class Lock(Base):
    def test_run_needs_a_lock(self):
        with self.assertRaisesRegex(ValueError, "lock"):
            weekly.run(self.dir, Fake())

    def test_lock_records_model_weeks_budget_criteria_and_context(self):
        e = self.lock()
        self.assertEqual((e["model"], e["first_week"], e["last_week"], e["budget"]), (MODEL, 2, 4, 50.0))
        self.assertEqual(e["context_hash"], weekly.context_hash())
        self.assertIn("min_weeks", e["criteria"])

    def test_weeks_stay_inside_the_open_weeks_and_after_week_one(self):
        with self.assertRaises(ValueError):
            self.lock(first_week=1)
        with self.assertRaises(ValueError):
            self.lock(last_week=20)

    def test_a_folder_used_for_rules_is_refused(self):
        self.play_week(2)
        with self.assertRaisesRegex(ValueError, "fresh replay folder"):
            self.lock()

    def test_one_lock_per_folder(self):
        self.lock()
        with self.assertRaises(ValueError):
            self.lock()

    def test_changed_instructions_after_the_lock_are_refused(self):
        self.lock()
        with mock.patch.object(weekly, "WEEK_INSTRUCTIONS", weekly.WEEK_INSTRUCTIONS + " x"):
            with self.assertRaisesRegex(ValueError, "changed after the lock"):
                weekly.run(self.dir, Fake())


class Order(Base):
    def setUp(self):
        super().setUp()
        self.lock()
        self.fake = Fake()
        weekly.run(self.dir, self.fake)

    def test_each_week_forecast_comes_before_its_days_and_days_are_one_per_day(self):
        kinds = [(k, w) for k, w, _ in self.fake.calls]
        weeks_seen = [w for k, w in kinds if k == "week"]
        self.assertEqual(weeks_seen, [2, 3, 4])
        first_week_call = [i for i, (k, w) in enumerate(kinds) if k == "week"]
        self.assertEqual(first_week_call[0], 0)
        days_between = [first_week_call[1] - first_week_call[0] - 1, first_week_call[2] - first_week_call[1] - 1]
        self.assertEqual(days_between, [WEEKS_DAYS, WEEKS_DAYS])
        self.assertEqual(len(kinds), 3 + 3 * WEEKS_DAYS)

    def test_the_forecast_is_in_the_ledger_before_the_result_of_its_week(self):
        kinds = [(e["kind"], e.get("week")) for e in replay.T.ledger_read(self.dir)
                 if e["kind"].startswith("agent_week_") and e["kind"] != "agent_week_lock"]
        self.assertEqual(kinds, [("agent_week_forecast", 2), ("agent_week_result", 2), ("agent_week_forecast", 3),
                                 ("agent_week_result", 3), ("agent_week_forecast", 4), ("agent_week_result", 4)])

    def test_the_weekly_prompt_shows_only_earlier_weeks(self):
        for k, w, p in self.fake.calls:
            if k != "week":
                continue
            market = p.split("Last week (week")[0]
            self.assertIn("Week %d:" % (w - 1), market)
            self.assertNotIn("Week %d:" % w, market)

    def test_the_notebook_carries_from_week_to_week_and_reaches_the_daily_choices(self):
        weeks = {w: p for k, w, p in self.fake.calls if k == "week"}
        self.assertIn("Your notebook now:\n(empty)", weeks[2])
        self.assertIn("Your notebook now:\nNB-2", weeks[3])
        self.assertIn("Your notebook now:\nNB-3", weeks[4])
        days = [p for k, _, p in self.fake.calls if k == "day"]
        self.assertIn("NB-2", days[0])
        self.assertIn("NB-4", days[-1])

    def test_last_weeks_result_and_choices_are_shown(self):
        p = [p for k, w, p in self.fake.calls if k == "week" and w == 3][0]
        self.assertIn("Your record so far:", p)
        self.assertIn("Week 2: you forecast", p)
        self.assertIn("Your choices last week", p)
        self.assertIn("Your reason: because", p)

    def test_no_names_dates_or_paths_reach_the_model(self):
        for _, _, p in self.fake.calls:
            self.assertNotIn("SECRET", p)
            self.assertNotIn(self.root, p)
            self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", p))

    def test_results_match_a_replay_of_the_logged_choices(self):
        plan, blind, weeks, _ = replay._open(self.dir)
        rows = sorted(self.entries("agent_week_result"), key=lambda e: e["week"])
        total = 1.0
        for r in rows:
            total *= 1.0 + r["strategy_return"]
            self.assertEqual(r["days_held"], WEEKS_DAYS)
            self.assertEqual(len(r["days"]), WEEKS_DAYS)
        self.assertAlmostEqual(weekly._rescore_agent(plan, blind, weeks, rows, plan["costs"]), total - 1.0, places=12)

    def test_costs_are_charged(self):
        rows = self.entries("agent_week_result")
        self.assertTrue(all(r["costs_paid"] > 0 and r["trades"] == WEEKS_DAYS for r in rows))
        plan, blind, weeks, _ = replay._open(self.dir)
        free = {k: 0.0 for k in COSTS}
        self.assertGreater(weekly._rescore_agent(plan, blind, weeks, rows, free),
                           weekly._rescore_agent(plan, blind, weeks, rows, plan["costs"]))

    def test_a_rerun_costs_nothing_and_changes_nothing(self):
        before = replay.T.ledger_read(self.dir)
        again = Fake()
        weekly.run(self.dir, again)
        self.assertEqual(again.calls, [])
        self.assertEqual(replay.T.ledger_read(self.dir), before)


class Choices(Base):
    def test_staying_out_every_day_scores_zero_and_is_judged_against_holding(self):
        self.lock(last_week=2)
        weekly.run(self.dir, Fake(day=lambda p: 0))
        r = self.entries("agent_week_result")[0]
        self.assertEqual(r["strategy_return"], 0.0)
        self.assertEqual(r["trades"], 0)
        for d in r["days"]:
            self.assertEqual(d["right"], d["hold_would_bps"] <= 0)

    def test_an_unusable_weekly_answer_keeps_the_notebook_and_is_marked(self):
        self.lock(last_week=3)
        texts = {2: json.dumps({"expect_return": 0.0, "expect_market": "up", "plan": "p", "notebook": "KEEP"}),
                 3: "no json here"}
        weekly.run(self.dir, Fake(week_text=lambda w: texts[w]))
        f = {e["week"]: e for e in self.entries("agent_week_forecast")}
        self.assertFalse(f[2]["failed"])
        self.assertTrue(f[3]["failed"])
        self.assertEqual(f[3]["notebook"], "KEEP")


class Budget(Base):
    def spent(self):
        return sum(e.get("cost", 0.0) for e in replay.T.ledger_read(os.path.join(self.dir, "agent")))

    def test_budget_stops_and_a_raised_budget_continues_from_the_saved_answers(self):
        # one Haiku call here costs 0.00035; the worst case of one call is about 0.009 (16000 output tokens)
        self.lock(budget=0.011)
        fake = Fake()
        with self.assertRaises(weekly.agent.BudgetExceeded):
            weekly.run(self.dir, fake)
        self.assertLessEqual(self.spent(), 0.011)
        done = len(fake.calls)
        self.assertGreater(done, 0)
        self.assertLess(len(self.entries("agent_week_result")), 3)
        with self.assertRaises(ValueError):
            weekly.raise_budget(self.dir, 0.005)
        weekly.raise_budget(self.dir, 1.0)
        more = Fake()
        weekly.run(self.dir, more)
        self.assertEqual(len(more.calls), 3 + 3 * WEEKS_DAYS - done)
        self.assertEqual(len(self.entries("agent_week_result")), 3)
        self.assertEqual(weekly.status(self.dir)["budget_dollars"], 1.0)


class Memory(Base):
    def setUp(self):
        super().setUp()
        self.lock()
        weekly.run(self.dir, Fake(day=lambda p: 1 if "Day 1" in p.split("Today so far")[0] else 0))
        self.mem = os.path.join(self.dir, "memory")

    def test_memory_files_are_written(self):
        for name in ("notebook.md", "notebook-history.md", "journal.md", "journal.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(self.mem, name)), name)

    def test_journal_has_every_week_every_day_reason_and_right_or_wrong(self):
        with open(os.path.join(self.mem, "journal.md")) as fh:
            text = fh.read()
        for w in (2, 3, 4):
            self.assertIn("## Week %d" % w, text)
            self.assertIn("plan %d" % w, text)
        self.assertIn("because", text)
        self.assertTrue("| right |" in text or "| wrong |" in text)
        with open(os.path.join(self.mem, "journal.jsonl")) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual([r["week"] for r in rows], [2, 3, 4])
        self.assertEqual(len(rows[0]["result"]["days"]), WEEKS_DAYS)

    def test_notebook_history_keeps_every_version(self):
        with open(os.path.join(self.mem, "notebook-history.md")) as fh:
            text = fh.read()
        for w in (2, 3, 4):
            self.assertIn("NB-%d" % w, text)

    def test_a_new_run_can_start_from_the_saved_notebook(self):
        d2 = os.path.join(self.root, "second")
        replay.start(source=self.csv, folder=d2, costs=COSTS, periods_per_year=self.PPY, days_per_week=WEEKS_DAYS,
                     holdout=0.34)
        weekly.lock(d2, MODEL, 50.0, 2, 2, notebook_from=os.path.join(self.mem, "notebook.md"))
        fake = Fake()
        weekly.run(d2, fake)
        self.assertIn("Your notebook now:\nNB-4", fake.calls[0][2])

    def test_memory_can_be_rebuilt_from_the_ledgers(self):
        os.remove(os.path.join(self.mem, "journal.md"))
        weekly.write_memory(self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.mem, "journal.md")))


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.lock()
        weekly.run(self.dir, Fake(day=lambda p: len(p) % 2))

    def test_agent_page_shows_results_without_names_dates_or_paths(self):
        page = weekly.render_agent(weekly.build(self.dir), "practice")
        self.assertIn("Agent replay", page)
        self.assertIn("API spend", page)
        self.assertIn("NB-4", page)
        self.assertNotIn("SECRET", page)
        self.assertNotIn(self.root, page)
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", page))

    def hand_run(self, weeks=(2, 3, 4)):
        hand = os.path.join(self.root, "hand")
        replay.start(source=self.csv, folder=hand, costs=COSTS, periods_per_year=self.PPY, days_per_week=WEEKS_DAYS,
                     holdout=0.34)
        for w in weeks:
            replay.predict(hand, w, "trend", {"window": 3}, 0.0, "up", "A: t")
            replay.reveal(hand, w)
        return hand

    def test_comparison_page_has_the_three_verdicts_and_both_runs(self):
        c = weekly.compare_build(self.dir, self.hand_run())
        self.assertEqual([t["against"] for t in c["tests"]],
                         ["the hand-played rules", "holding the stock", "staying out"])
        self.assertTrue(all(t["verdict"] == "INCONCLUSIVE" for t in c["tests"]))   # 3 weeks < 20
        page = weekly.render_compare(c, "practice")
        self.assertIn("Agent versus rules", page)
        self.assertIn("trend (window=3)", page)
        self.assertNotIn("SECRET", page)
        self.assertNotIn(self.root, page)

    def test_comparison_refuses_a_rules_run_on_other_data_or_costs(self):
        other = os.path.join(self.root, "other")
        replay.start(source=self.csv, folder=other, costs=dict(COSTS, spread_bps=9.0), periods_per_year=self.PPY,
                     days_per_week=WEEKS_DAYS, holdout=0.34)
        with self.assertRaisesRegex(ValueError, "costs"):
            weekly.compare_build(self.dir, other)

    def test_comparison_refuses_missing_weeks(self):
        with self.assertRaisesRegex(ValueError, "no result"):
            weekly.compare_build(self.dir, self.hand_run(weeks=(2, 3)))

    def test_command_line_report_and_compare(self):
        hand = self.hand_run()
        for args in (["report", "--dir", self.dir, "--out", os.path.join(self.root, "a.html")],
                     ["compare", "--dir", self.dir, "--rules-dir", hand, "--out", os.path.join(self.root, "c.html")],
                     ["status", "--dir", self.dir], ["memory", "--dir", self.dir]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(weekly.main(args), 0, args)
        self.assertTrue(os.path.exists(os.path.join(self.root, "c.html")))


class Overnight(Base):
    def run_overnight(self, day=lambda p: 1):
        self.lock(overnight=True)
        fake = Fake(day=day)
        weekly.run(self.dir, fake)
        return fake, sorted(self.entries("agent_week_result"), key=lambda e: e["week"])

    def test_the_mode_is_locked_and_changes_the_context(self):
        e = self.lock(overnight=True)
        self.assertTrue(e["overnight"])
        self.assertEqual(e["context_hash"], weekly.context_hash(True))
        self.assertNotEqual(weekly.context_hash(True), weekly.context_hash())

    def test_a_normal_lock_keeps_the_old_context_so_old_runs_still_continue(self):
        e = self.lock()
        self.assertFalse(e["overnight"])
        self.assertEqual(e["context_hash"], weekly.context_hash())

    def test_always_holding_buys_once_and_keeps_the_stock_overnight_and_over_weekends(self):
        fake, rows = self.run_overnight()
        self.assertEqual(sum(r["trades"] for r in rows), 0)          # bought once, never sold
        self.assertGreater(rows[0]["costs_paid"], 0.0)
        for r in rows[1:]:
            self.assertEqual(r["costs_paid"], 0.0)
            self.assertAlmostEqual(r["strategy_return"], r["stock_move"], places=9)   # the whole move, gaps too

    def test_the_model_is_told_the_rules_and_what_it_holds(self):
        fake, rows = self.run_overnight()
        days = [p for kind, _, p in fake.calls if kind == "day"]
        self.assertTrue(all(p.startswith(weekly.DAY_INSTRUCTIONS_OVERNIGHT) for p in days))
        self.assertIn("You own now: cash", days[0])
        self.assertIn("You own now: all in (kept from your last decision)", days[1])
        weeks = [p for kind, _, p in fake.calls if kind == "week"]
        self.assertTrue(all(p.startswith(weekly.WEEK_INSTRUCTIONS_OVERNIGHT) for p in weeks))
        self.assertIn("to your next decision", weeks[-1])

    def test_costs_only_when_the_position_changes(self):
        fake, rows = self.run_overnight(day=lambda p: len(p) % 2)
        for r in rows:
            for d in r["days"]:
                if d.get("position") is None:
                    continue
                if d["position"] == d["kept_from_before"]:
                    self.assertAlmostEqual(d["result_bps"], d["move_bps"] if d["position"] else 0.0, places=9)

    def test_rescoring_and_money_follow_the_real_path(self):
        fake, rows = self.run_overnight(day=lambda p: len(p) % 2)
        plan, blind, weeks, _ = replay._open(self.dir)
        total = 1.0
        for r in rows:
            total *= 1.0 + r["strategy_return"]
        self.assertAlmostEqual(weekly._rescore_agent(plan, blind, weeks, rows, plan["costs"], True), total - 1.0,
                               places=12)
        d = weekly.build(self.dir)
        self.assertAlmostEqual(d["agent_total"], total - 1.0, places=12)
        m = weekly.agent_money(self.dir, d, 500.0)
        self.assertAlmostEqual(m["agent_end"], 500.0 * total, places=9)
        self.assertIn("overnight", weekly.render_agent(d, start_money=m))

    def test_command_line_lock(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(weekly.main(["lock", "--dir", self.dir, "--model", MODEL, "--budget-usd", "1",
                                          "--last-week", "4", "--overnight"]), 0)
        self.assertIn('"overnight": true', out.getvalue())


class Version2(Base):
    def test_forecasts_snap_to_the_steps(self):
        for given, step in ((0.0007, 0.0), (0.004, 0.005), (-0.02, -0.01), (0.0025, 0.0), (0.008, 0.01)):
            got = weekly.parse_week(json.dumps({"expect_return": given, "expect_market": "up", "notebook": ""}))
            self.assertEqual(got["expect_return"], step, given)

    def test_half_is_a_valid_answer_and_other_sizes_are_not(self):
        self.assertEqual(weekly.parse_day('{"position": 0.5, "reason": "x"}')["position"], 0.5)
        self.assertEqual(weekly.parse_day('{"position": 1, "reason": "x"}')["position"], 1)
        for bad in ('{"position": 0.3}', '{"position": true}', '{"position": "1"}'):
            with self.assertRaises(ValueError):
                weekly.parse_day(bad)

    def test_the_lock_records_the_version_and_when_to_decide(self):
        e = self.lock(overnight=True)
        self.assertEqual(e["version"], weekly.WEEK_VERSION)
        self.assertEqual(e["decide_at_bar"], e["day_bars"] - 1)
        e2 = weekly.lock(folder=self.start_other(), model=MODEL, budget=1.0, first_week=2, last_week=3)
        self.assertEqual(e2["decide_at_bar"], weekly.agent.DECISION_BAR)

    def start_other(self):
        other = os.path.join(self.root, "other")
        replay.start(source=self.csv, folder=other, costs=COSTS, periods_per_year=self.PPY, days_per_week=WEEKS_DAYS,
                     holdout=0.34)
        return other

    def test_overnight_decides_before_the_last_bar_of_the_day(self):
        e = self.lock(overnight=True)
        weekly.run(self.dir, Fake())
        plan, blind, weeks, _ = replay._open(self.dir)
        for r in self.entries("agent_week_result"):
            for d in r["days"]:
                if d.get("position") is None:
                    continue
                i = d["decided_at"]
                day = blind[i]["t"][:10]
                k = sum(1 for j in range(max(0, i - 50), i + 1) if blind[j]["t"][:10] == day)
                self.assertEqual(k, e["decide_at_bar"])

    def test_half_size_runs_are_scored_and_replayed_the_same(self):
        self.lock(overnight=True)
        weekly.run(self.dir, Fake(day=lambda p: 0.5 if len(p) % 3 else 1))
        rows = sorted(self.entries("agent_week_result"), key=lambda e: e["week"])
        self.assertTrue(any(d.get("position") == 0.5 for r in rows for d in r["days"]))
        plan, blind, weeks, _ = replay._open(self.dir)
        total = 1.0
        for r in rows:
            total *= 1.0 + r["strategy_return"]
        self.assertAlmostEqual(weekly._rescore_agent(plan, blind, weeks, rows, plan["costs"], True, True),
                               total - 1.0, places=12)
        d = weekly.build(self.dir)
        self.assertAlmostEqual(weekly.agent_money(self.dir, d, 500.0)["agent_end"], 500.0 * total, places=9)

    def test_half_size_in_a_day_only_run(self):
        self.lock()
        weekly.run(self.dir, Fake(day=lambda p: 0.5))
        rows = self.entries("agent_week_result")
        plan, blind, weeks, _ = replay._open(self.dir)
        total = 1.0
        for r in sorted(rows, key=lambda e: e["week"]):
            total *= 1.0 + r["strategy_return"]
        self.assertAlmostEqual(weekly._rescore_agent(plan, blind, weeks, rows, plan["costs"], False, True),
                               total - 1.0, places=12)

    def test_the_weekly_prompt_shows_missed_gains_patterns_and_four_weeks(self):
        self.lock(overnight=True, last_week=7)
        fake = Fake(day=lambda p: 0)
        weekly.run(self.dir, fake)
        last = [p for kind, w, p in fake.calls if kind == "week"][-1]
        self.assertIn("simply holding the stock", last)
        self.assertIn("so your choices", last)
        self.assertIn("close to next open (overnight)", last)
        self.assertIn("opening gap <= -50 bps", last)
        for k in (3, 4, 5, 6):
            self.assertIn("Week %d day by day:" % k, last)
        self.assertNotIn("Week 2 day by day:", last)
        self.assertIn("checkable from what you are shown", last)
        self.assertNotIn("Staying in cash is a valid choice and costs nothing", weekly.WEEK_INSTRUCTIONS)

    def test_the_pattern_table_counts_only_weeks_already_seen(self):
        plan, blind, weeks, _ = replay._open(self.dir)
        early = weekly.pattern_table(blind, weeks, 3)
        n = int(re.search(r"n=(\d+)", early[1]).group(1))
        self.assertEqual(n, 2 * WEEKS_DAYS)

    def test_a_run_locked_with_an_older_version_is_refused(self):
        self.lock()
        old = weekly.WEEK_VERSION
        try:
            weekly.WEEK_VERSION = "3"
            with self.assertRaisesRegex(ValueError, "version 2"):
                weekly.run(self.dir, Fake())
        finally:
            weekly.WEEK_VERSION = old


if __name__ == "__main__":
    unittest.main()


class Money(Pages):
    def test_agent_money_follows_its_result_from_500(self):
        d = weekly.build(self.dir)
        m = weekly.agent_money(self.dir, d, 500.0)
        self.assertAlmostEqual(m["agent_end"], 500.0 * (1.0 + d["agent_total"]), places=9)
        self.assertGreater(m["agent_costs"], 0.0)
        page = weekly.render_agent(d, start_money=m)
        self.assertIn("Your money", page)
        self.assertIn(weekly.report.eur(m["agent_end"]), page)

    def test_comparison_money_for_every_arm_over_the_same_weeks(self):
        c = weekly.compare_build(self.dir, self.hand_run(weeks=(2, 3, 4, 5)), start_money=500.0)
        m = c["money"]
        self.assertAlmostEqual(m["agent_end"], 500.0 * (1.0 + c["agent_total"]), places=9)
        self.assertAlmostEqual(m["rules_end"], 500.0 * (1.0 + c["rules_total"]), places=9)   # weeks 2-4 only
        self.assertAlmostEqual(m["stock_end"], 500.0 * (1.0 + c["stock_total"]), places=9)
        page = weekly.render_compare(c)
        for v in (m["agent_end"], m["rules_end"], m["stock_end"]):
            self.assertIn(weekly.report.eur(v), page)
