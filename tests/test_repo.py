import json
import os
import re
import unittest

from _load import ROOT

SKILLS = ["trader-copycat", "trader-news", "trader-budget", "trader-insiders", "trader-analyst",
          "trader-screener", "trader-risk", "trader-broker", "trader-tax", "trader-alerts",
          "trader-rebalance", "trader-backtest", "trader-data", "trader-strategies"]
ORDER_PATH = ["trader-copycat", "trader-budget", "trader-risk", "trader-broker",
              "trader-tax", "trader-alerts", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def text_files(skip=("docs", "tests", ".git", "__pycache__")):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith((".md", ".py", ".json")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_skill_folders(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, _ = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_no_plugin_manifest_so_nothing_auto_updates_from_upstream(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".claude-plugin")))

    def test_no_upstream_install_or_update_instructions(self):
        bad = ("plugin marketplace add", "plugin install trader-skill", "install skill")
        for path in text_files():
            with open(path, encoding="utf-8", errors="replace") as fh:
                low = fh.read().lower()
            for b in bad:
                self.assertNotIn(b, low, "%s mentions %r" % (os.path.relpath(path, ROOT), b))

    def test_no_us_only_leftovers_in_the_european_edition(self):
        bad = ("pelosi", "lots.py", "wash sale", "short term", "long term", "_usd", "irs ", "ptr.py")
        for path in text_files():
            with open(path, encoding="utf-8", errors="replace") as fh:
                low = fh.read().lower()
            for b in bad:
                self.assertNotIn(b, low, "%s still mentions %r" % (os.path.relpath(path, ROOT), b))

    def test_readme_lists_every_skill(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        for s in SKILLS:
            self.assertIn(s, text, "README does not mention %s" % s)

    def test_europe_doc_exists_and_names_its_sources(self):
        with open(os.path.join(ROOT, "docs", "EUROPE.md"), encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("§ 32d", "§ 20 Abs. 4", "§ 43a", "InvStG", "VIII R 31/23", "2 BvL 3/21", "Deutsche Boerse"):
            self.assertIn(needle, text)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_options", False), False)


if __name__ == "__main__":
    unittest.main()
