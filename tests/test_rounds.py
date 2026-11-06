import datetime
import io
import json
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load
from test_weekly import Fake

rounds = load("trader-agent", "rounds")
weekly = rounds.weekly
replay = rounds.replay

COSTS = {"commission_bps": 9.0, "spread_bps": 4.0, "slippage_bps": 2.0, "fixed_fee": 0.0}


def crypto_csv(path, seed, weeks=12):
    rnd = random.Random(seed)
    day, px, rows = datetime.date(2024, 1, 1), 100.0, []
    for d in range(7 * weeks):
        for h in range(24):
            o = px
            px = o * (1 + rnd.gauss(0, 0.004))
            rows.append("%sT%02d:00:00Z,%.6f,%.6f,%.6f,%.6f,%d" % (day.isoformat(), h, o, max(o, px) * 1.001,
                                                                   min(o, px) * 0.999, px, rnd.randint(0, 50)))
        day += datetime.timedelta(days=1)
    with open(path, "w") as fh:
        fh.write("t,o,h,l,c,v\n" + "\n".join(rows) + "\n")
    return path


class Rounds(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.base, "history"))
        self.assets = [(s, crypto_csv(os.path.join(self.base, "history", s + ".csv"), k))
                       for k, s in enumerate(("AAA-EUR", "BBB-EUR"), 1)]

    def make(self, **kw):
        args = dict(base=self.base, name="c1", assets=self.assets, market="crypto", costs=COSTS,
                    periods_per_year=8760, days_per_week=7, model="claude-haiku-5-5", budget=5.0)
        args.update(kw)
        return rounds.setup(**args)

    def play(self, cfg, weeks_rules=None):
        root = os.path.join(self.base, "c1")
        for a in cfg["assets"]:
            weekly.run(os.path.join(root, a["agent"]), Fake(day=lambda p: 0.5))
            last = a["last_week"] if weeks_rules is None else weeks_rules
            for w in range(a["first_week"], last + 1):
                replay.predict(os.path.join(root, a["rules"]), w, "hold", {}, 0.0, "up")
                replay.reveal(os.path.join(root, a["rules"]), w)

    def test_setup_hides_the_names_from_the_rules_side_and_locks_the_agents(self):
        cfg = self.make()
        root = os.path.join(self.base, "c1")
        self.assertEqual(sorted(os.listdir(root)), sorted(["agent-aaa-eur", "agent-bbb-eur", "rules-s1", "rules-s2",
                                                           rounds.CONFIG, rounds.KEY]))
        self.assertEqual(sorted(os.listdir(os.path.join(self.base, "c1-data"))), ["s1.csv", "s2.csv"])
        with open(os.path.join(root, rounds.KEY)) as fh:
            key = fh.read()
        self.assertIn("AAA-EUR", key)
        self.assertIn("BBB-EUR", key)
        for a in cfg["assets"]:
            plan, _, _, entries = replay._open(os.path.join(root, a["rules"]))
            self.assertTrue(replay.overnight(plan))
            lk = [e for e in replay.T.ledger_read(os.path.join(root, a["agent"])) if e["kind"] == "agent_week_lock"][0]
            self.assertEqual((lk["market"], lk["overnight"]), ("crypto", True))

    def test_a_round_is_never_set_up_twice(self):
        self.make()
        with self.assertRaisesRegex(ValueError, "never set up twice"):
            self.make()

    def test_save_skips_unfinished_assets_and_saves_the_rest_as_one_round(self):
        cfg = self.make()
        self.play(cfg)
        reports = os.path.join(self.base, "reports")
        saved, skipped = rounds.save(self.base, "c1", reports, 500.0)
        self.assertEqual(skipped, [])
        self.assertEqual(sorted(e["file"] for e in saved), sorted(
            ["rules-run-01.html", "rules-run-02.html", "agent-run-01.html", "agent-run-02.html",
             "comparison-01.html", "comparison-02.html"]))
        with open(os.path.join(reports, "index.html")) as fh:
            self.assertIn("Round: c1", fh.read())

    def test_unfinished_rules_are_skipped(self):
        cfg = self.make()
        self.play(cfg, weeks_rules=3)
        saved, skipped = rounds.save(self.base, "c1", os.path.join(self.base, "reports"), 500.0)
        self.assertEqual(saved, [])
        self.assertEqual(len(skipped), 2)

    def test_command_line(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rounds.main(["setup", "--base", self.base, "--name", "c1", "--market", "crypto",
                                          "--asset", "AAA-EUR=history/AAA-EUR.csv", "--asset",
                                          "BBB-EUR=history/BBB-EUR.csv", "--commission-bps", "9", "--spread-bps",
                                          "4", "--slippage-bps", "2", "--periods-per-year", "8760",
                                          "--days-per-week", "7", "--model", "claude-haiku-5-5",
                                          "--budget-usd", "1"]), 0)
            self.assertEqual(rounds.main(["status", "--base", self.base, "--name", "c1"]), 0)
        self.assertIn("rules-s1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
