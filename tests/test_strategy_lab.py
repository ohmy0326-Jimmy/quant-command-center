"""交易策略實驗室網址只定義一次。"""

import unittest
from pathlib import Path

from strategy_lab import STRATEGY_LAB_URL

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "https://app.notion.com/p/4103a4a1034a810fa11cc72ff0b139ae?pvs=12"


class StrategyLabUrlTests(unittest.TestCase):
    def test_strategy_lab_url_constant(self):
        self.assertEqual(STRATEGY_LAB_URL, EXPECTED)

    def test_dashboard_uses_the_constant_once(self):
        dashboard = (ROOT / "Dashboard.py").read_text(encoding="utf-8")
        module = (ROOT / "strategy_lab.py").read_text(encoding="utf-8")
        self.assertEqual(module.count(EXPECTED), 1)
        self.assertNotIn(EXPECTED, dashboard)
        self.assertNotIn("https://www.notion.so/", dashboard)
        self.assertIn("STRATEGY_LAB_URL", dashboard)
