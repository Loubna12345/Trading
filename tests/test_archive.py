import io
import os
import unittest
from contextlib import redirect_stdout

from _load import load
from test_replay import COSTS, WEEKS_DAYS
from test_weekly import Base, Fake

replay = load("trader-replay", "replay")
weekly = load("trader-agent", "weekly")
archive = load("trader-agent", "archive")


class Archive(Base):
    def setUp(self):
        super().setUp()
        self.lock()
        weekly.run(self.dir, Fake(day=lambda p: len(p) % 2))
        self.hand = os.path.join(self.root, "hand")
        replay.start(source=self.csv, folder=self.hand, costs=COSTS, periods_per_year=self.PPY,
                     days_per_week=WEEKS_DAYS, holdout=0.34)
        for w in (2, 3, 4):
            self.play_hand(w)
        self.reports = os.path.join(self.root, "reports")

    def play_hand(self, w):
        replay.predict(self.hand, w, "trend", {"window": 3}, 0.0, "up", "A: t")
        replay.reveal(self.hand, w)

    def test_each_kind_is_numbered_on_its_own(self):
        r = archive.save_rules(self.reports, self.hand, 500.0)
        a = archive.save_agent(self.reports, self.dir, 500.0)
        c = archive.save_comparison(self.reports, self.dir, self.hand, 500.0)
        self.assertEqual((r["file"], a["file"], c["file"]), ("rules-run-01.html", "agent-run-01.html",
                                                             "comparison-01.html"))
        for e in (r, a, c):
            self.assertTrue(os.path.exists(os.path.join(self.reports, e["file"])))
        self.assertEqual((c["rules_run"], c["agent_run"]), (1, 1))

    def test_the_same_run_state_is_not_saved_twice(self):
        archive.save_rules(self.reports, self.hand, 500.0)
        with self.assertRaisesRegex(ValueError, "already saved as rules-run-01"):
            archive.save_rules(self.reports, self.hand, 500.0)

    def test_a_later_state_gets_a_new_number_and_the_old_file_is_untouched(self):
        first = archive.save_rules(self.reports, self.hand, 500.0)
        path = os.path.join(self.reports, first["file"])
        with open(path, "rb") as fh:
            before = fh.read()
        self.play_hand(5)
        second = archive.save_rules(self.reports, self.hand, 500.0)
        self.assertEqual(second["file"], "rules-run-02.html")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_an_existing_file_is_never_overwritten(self):
        os.makedirs(self.reports)
        with open(os.path.join(self.reports, "rules-run-01.html"), "w") as fh:
            fh.write("mine")
        with self.assertRaisesRegex(ValueError, "already exists"):
            archive.save_rules(self.reports, self.hand, 500.0)
        with open(os.path.join(self.reports, "rules-run-01.html")) as fh:
            self.assertEqual(fh.read(), "mine")
        self.assertEqual(replay.T.ledger_read(self.reports), [])

    def test_the_overview_lists_every_run_with_its_money(self):
        archive.save_rules(self.reports, self.hand, 500.0)
        a = archive.save_agent(self.reports, self.dir, 500.0, label="first try")
        with open(os.path.join(self.reports, "index.html")) as fh:
            page = fh.read()
        self.assertIn("rules-run-01.html", page)
        self.assertIn("agent-run-01.html", page)
        self.assertIn("first try", page)
        self.assertIn(weekly.report.eur(a["end_money"]), page)
        self.assertEqual(len(replay.T.ledger_read(self.reports)), 2)

    def test_command_line(self):
        for args in (["rules", "--dir", self.hand, "--reports", self.reports, "--start-money", "500"],
                     ["agent", "--dir", self.dir, "--reports", self.reports, "--start-money", "500"],
                     ["compare", "--dir", self.dir, "--rules-dir", self.hand, "--reports", self.reports,
                      "--start-money", "500"],
                     ["index", "--reports", self.reports]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(archive.main(args), 0, args)
        self.assertEqual(sorted(f for f in os.listdir(self.reports) if f.endswith(".html")),
                         ["agent-run-01.html", "comparison-01.html", "index.html", "rules-run-01.html"])


if __name__ == "__main__":
    unittest.main()
