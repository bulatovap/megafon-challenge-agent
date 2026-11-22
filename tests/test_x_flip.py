"""Тест на разворот: skip/run, запись calc.skipped с экономией, порог и диапазон в фразе, MDE из calc.py, отчёт."""
from __future__ import annotations

import unittest

from x_fixtures import XCase

from tools.bulochka import journal, x_econ


class TestFlip(XCase):
    def setUp(self):
        super().setUp()
        self.journal("situation", "2026-11-09T09:00:00Z")

    def flip(self, *extra):
        r, out = self.cli_json("flip", "--question", "каннибализация тарифов", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out["data"]

    def test_skip_when_range_below_switch(self):
        d = self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        self.assertEqual(d["decision"], "skip")
        ev = [r for r in journal.records(self.slug) if r.get("operation") == "calc.skipped"]
        self.assertEqual(len(ev), 1)
        self.assertGreater(ev[0]["saved_tokens"], 0)

    def test_skip_phrase_has_threshold_and_range(self):
        d = self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        self.assertIn("≥12 п.п.", d["phrase"])
        self.assertIn("правдоподобный — 0–3 п.п.", d["phrase"])
        self.assertIn("не меняет выбор ни при каком исходе", d["phrase"])
        self.assertIn("из стоимости подписки", d["phrase"])

    def test_run_when_range_crosses(self):
        d = self.flip("--switch", "12 п.п.", "--plausible", "5..15 п.п.", "--if-above", "O2", "--if-below", "O1")
        self.assertEqual(d["decision"], "run")
        self.assertEqual(sorted(d["outcomes"].values()), ["O1", "O2"])
        self.assertIn("O2", d["phrase"])
        ev = [r for r in journal.records(self.slug) if r.get("operation") == "calc.skipped"]
        self.assertEqual(ev, [])

    def test_skip_when_range_all_above(self):
        d = self.flip("--switch", "12 п.п.", "--plausible", "14..20 п.п.", "--if-above", "O2")
        self.assertEqual(d["decision"], "skip")
        self.assertEqual(d["outcome"], "O2")

    def test_percent_units(self):
        d = self.flip("--switch", "5%", "--plausible", "1..3%")
        self.assertEqual(d["decision"], "skip")

    def test_calc_mde_attached(self):
        d = self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.", "--calc", "mde --base 0.04 --lift 0.1")
        self.assertIsNotNone(d["calc"]["mde_pp"])
        self.assertIn("MDE", d["phrase"])

    def test_saved_tokens_from_history(self):
        x_econ.append_history({"run_id": "r1", "tokens": 900000, "calc_tokens": 35000, "weekly_delta": 1.5})
        d = self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        self.assertEqual(d["saved_tokens"], 35000)
        self.assertIsNotNone(d["saved_rub"])

    def test_report(self):
        self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        self.flip("--switch", "12 п.п.", "--plausible", "5..15 п.п.")
        r, out = self.cli_json("flip", "report")
        self.assertEqual(out["data"]["skipped"], 1)
        self.assertEqual(out["data"]["ran"], 1)
        text = (self.root / "projects" / self.slug / "report" / "flip_tests.md").read_text(encoding="utf-8")
        self.assertIn("Расчётов пропущено: 1", text)

    def test_each_flip_rebuilds_report_without_flip_report(self):
        out = self.root / "projects" / self.slug / "report" / "flip_tests.md"
        self.assertFalse(out.exists())
        d = self.flip("--switch", "1 млн", "--plausible", "0,5..3 млн", "--if-above", "O2", "--if-below", "O1")
        self.assertEqual(d["decision"], "run")
        self.assertTrue(out.exists(), "flip сам пишет report/flip_tests.md")
        self.assertIn("посчитано после проверки: 1", out.read_text(encoding="utf-8"))
        self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        text = out.read_text(encoding="utf-8")
        self.assertIn("Расчётов пропущено: 1", text)
        self.assertIn("посчитано после проверки: 1", text)

    def test_repeated_skip_not_double_counted(self):
        self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        d = self.flip("--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        self.assertTrue(d["repeated"])
        ev = [r for r in journal.records(self.slug) if r.get("operation") == "calc.skipped"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["step"], "situation")  # шаг события = шаг учёта токенов

    def test_bad_input_is_usage_error(self):
        r = self.run_cli("flip", "--switch", "много", "--plausible", "0..3")
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()
