"""scripts/calc.py: MDE, SRM, когорты, LTV, plan_hash, CLI и модельные данные B01."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALC = ROOT / "scripts" / "calc.py"
DATA = ROOT / "knowledge" / "briefs" / "B01_data"

_spec = importlib.util.spec_from_file_location("bulochka_calc", CALC)
calc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calc)  # type: ignore[union-attr]

BANNER = "МОДЕЛЬНЫЕ ДАННЫЕ для демонстрации, не данные МегаФона и не реальные пользователи"


def run_cli(*args):
    return subprocess.run([sys.executable, str(CALC), *args], capture_output=True, text=True, cwd=str(ROOT))


class MdeTest(unittest.TestCase):
    def test_reference_4pct_10pct(self):
        n = calc.sample_size_two_props(0.04, 0.10)["n_per_variant"]
        self.assertAlmostEqual(n, 39475, delta=39475 * 0.01)

    def test_reference_table_from_methodology(self):
        self.assertAlmostEqual(calc.sample_size_two_props(0.30, 0.10)["n_per_variant"], 3800, delta=3800 * 0.03)
        self.assertAlmostEqual(calc.sample_size_two_props(0.30, 0.05)["n_per_variant"], 14900, delta=14900 * 0.02)

    def test_abs_lift_equals_relative(self):
        rel = calc.sample_size_two_props(0.04, 0.10)
        ab = calc.sample_size_two_props(0.04, 0.004, relative=False)
        self.assertEqual(rel["n_per_variant"], ab["n_per_variant"])

    def test_stricter_alpha_and_power_need_more(self):
        base = calc.sample_size_two_props(0.04, 0.10)["n_per_variant"]
        self.assertGreater(calc.sample_size_two_props(0.04, 0.10, alpha=0.01)["n_per_variant"], base)
        self.assertGreater(calc.sample_size_two_props(0.04, 0.10, power=0.9)["n_per_variant"], base)

    def test_duration_rounds_to_full_weeks_with_two_week_floor(self):
        d = calc.duration(39475, 20000)          # 78 950 / 20 000 → 4 дн. → 1 нед. → минимум 2 нед.
        self.assertEqual((d["days_raw"], d["weeks"], d["days"], d["floor_applied"]), (4, 2, 14, True))
        d = calc.duration(39475, 10000)          # 8 дн. → 2 нед. без подъёма
        self.assertEqual((d["days_raw"], d["weeks"], d["days"], d["floor_applied"]), (8, 2, 14, False))
        d = calc.duration(39475, 3000)           # 27 дн. → 4 нед.
        self.assertEqual((d["days_raw"], d["weeks"], d["days"], d["floor_applied"]), (27, 4, 28, False))

    def test_bad_inputs(self):
        for kw in ({"base": 4, "lift": 0.1}, {"base": 0.04, "lift": 0}, {"base": 0.9, "lift": 0.2}):
            with self.assertRaises(calc.CalcError):
                calc.sample_size_two_props(**kw)
        with self.assertRaises(calc.CalcError):
            calc.duration(100, 0)


class SrmTest(unittest.TestCase):
    def test_known_chi2_and_p(self):
        r = calc.srm_test(5100, 4900)
        self.assertAlmostEqual(r["chi2"], 4.0, places=6)
        self.assertAlmostEqual(r["p_value"], 0.0455, places=4)
        self.assertFalse(r["srm"])

    def test_detects_mismatch(self):
        r = calc.srm_test(50500, 49000)
        self.assertLess(r["p_value"], 0.001)
        self.assertTrue(r["srm"])

    def test_unequal_ratio(self):
        r = calc.srm_test(9000, 1000, ratio=0.9)
        self.assertAlmostEqual(r["chi2"], 0.0, places=9)
        self.assertAlmostEqual(r["p_value"], 1.0, places=9)


class PlanHashTest(unittest.TestCase):
    def test_deterministic_and_input_sensitive(self):
        h1 = calc.plan_hash("mde", {"base": 0.04, "lift": 0.1})
        self.assertEqual(h1, calc.plan_hash("mde", {"lift": 0.1, "base": 0.04}))
        self.assertNotEqual(h1, calc.plan_hash("mde", {"base": 0.04, "lift": 0.11}))
        self.assertNotEqual(h1, calc.plan_hash("srm", {"base": 0.04, "lift": 0.1}))
        self.assertEqual(len(h1), 64)

    def test_depends_on_version(self):
        h1 = calc.plan_hash("mde", {"base": 0.04})
        old = calc.VERSION
        try:
            calc.VERSION = "calc/9.9"
            self.assertNotEqual(h1, calc.plan_hash("mde", {"base": 0.04}))
        finally:
            calc.VERSION = old


class CohortTest(unittest.TestCase):
    CSV = """# комментарий
cohort,segment,trials,paid,m1,m2,m3,m4
2026-01,a,1000,500,400,350,345,340
2026-02,a,1000,500,400,,345,
2026-01,b,1000,200,100,60,40,25
2026-03,b,1000,200,100,,,
2026-03,a,1000,500,,,,
"""

    def analyse(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_text(self.CSV, encoding="utf-8")
            rows, _ = calc._read_csv(p)
        return calc.cohort_analysis(rows)

    def test_gap_holes_missing(self):
        r = self.analyse()
        self.assertEqual(r["segment_gap"][0]["gap_pp"], 30.0)   # M1: 80% против 50%
        self.assertTrue(r["max_gap"]["flag"])
        self.assertIn({"segment": "a", "cohort": "2026-02", "month": "M2"}, r["holes"])
        self.assertIn({"segment": "b", "cohort": "2026-02"}, r["missing_cohorts"])
        self.assertEqual(r["segments"]["a"]["trial_to_paid"], 0.5)

    def test_shapes(self):
        r = self.analyse()
        self.assertEqual(r["segments"]["a"]["shape"], "плато")
        self.assertEqual(r["segments"]["b"]["shape"], "ведро")
        self.assertEqual(calc.classify_shape([1, 0.5, 0.35, 0.3, 0.34, 0.36])["shape"], "улыбка")
        self.assertEqual(calc.classify_shape([1, 0.5, None])["shape"], "мало данных")


class LtvTest(unittest.TestCase):
    def test_ltv_and_ratio(self):
        r = calc.ltv_calc(399, 0.3, 0.1, cac=600)
        self.assertAlmostEqual(r["ltv"], 1197.0)
        self.assertAlmostEqual(r["lifetime_months"], 10.0)
        self.assertAlmostEqual(r["ltv_cac"], 1.995)
        self.assertAlmostEqual(r["payback_months"], 600 / 119.7, places=3)
        with self.assertRaises(calc.CalcError):
            calc.ltv_calc(399, 30, 0.1)


class CliTest(unittest.TestCase):
    def test_json_has_inputs_and_plan_hash(self):
        p = run_cli("mde", "--base", "0.04", "--lift", "0.10", "--daily", "5000", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["inputs"]["base"], 0.04)
        self.assertEqual(d["plan_hash"], calc.plan_hash("mde", d["inputs"]))
        self.assertIn("--plan-hash " + d["plan_hash"], d["claim_hint"])
        self.assertEqual(d["result"]["duration"]["days"] % 7, 0)

    def test_human_default_and_usage_error(self):
        p = run_cli("srm", "--a", "5100", "--b", "4900")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("χ²", p.stdout)
        self.assertIn("plan_hash:", p.stdout)
        bad = run_cli("ltv", "--arpu", "399", "--margin", "0.3", "--churn", "5", "--json")
        self.assertEqual(bad.returncode, 3)
        self.assertIn("error", json.loads(bad.stdout))


class B01DataTest(unittest.TestCase):
    def test_banner_everywhere_and_answers_not_linked(self):
        for name in ("cohorts.csv", "cancel_reasons.csv", "nps.csv", "README.md"):
            self.assertIn(BANNER, (DATA / name).read_text(encoding="utf-8"), name)
        # ключ ответов хранится вне продукта, агент не должен его видеть
        self.assertFalse((DATA / "ANSWERS.md").exists())
        brief = (ROOT / "knowledge" / "briefs" / "B01.md").read_text(encoding="utf-8")
        self.assertIn("B01_data", brief)
        self.assertNotIn("ANSWERS", brief)

    def test_traps_visible_to_calc(self):
        rows, _ = calc._read_csv(DATA / "cohorts.csv")
        r = calc.cohort_analysis(rows)
        self.assertEqual(len({t["cohort"] for t in r["rows"]}), 12)
        self.assertGreater(r["segment_gap"][0]["gap_pp"], 20)            # (а) уже на M1
        self.assertEqual(r["segments"]["промо"]["shape"], "ведро")
        self.assertIn({"segment": "партнёры", "cohort": "2026-02"}, r["missing_cohorts"])  # (в)
        self.assertTrue(r["holes"])
        header = (DATA / "cancel_reasons.csv").read_text(encoding="utf-8")
        self.assertIn("30 ДНЕЙ ОТ НАЧАЛА ТРИАЛА", header)                 # (б)
        self.assertIn("КАЛЕНДАРНОГО", (DATA / "cohorts.csv").read_text(encoding="utf-8"))


class SchetTest(unittest.TestCase):
    """calc.py schet: арифметика по числам брифа без python3 -c и своих скриптов."""

    def test_expressions(self):
        self.assertEqual(calc.schet("(plan - fact) / plan * 100", {"plan": 5000, "fact": 4000}), 20)
        self.assertEqual(calc.schet("2 ** 10 + 7 // 2 - 7 % 2"), 1026)
        self.assertEqual(calc.schet("max(a, b) - min(a, b) + abs(-3)", {"a": 10, "b": 4}), 9)
        self.assertEqual(calc.schet("sum([1, 2, 3.5]) * -1"), -6.5)
        self.assertEqual(calc.schet("round(282 - 275.4, 1)"), 6.6)

    def test_cli_shows_formula_substitution_and_result(self):
        r = run_cli("schet", "(plan - fact) / plan", "--var", "plan=5 000", "--var", "fact=4000",
                    "--label", "отставание", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["result"]["value"], 0.2)
        self.assertEqual(d["result"]["formula"], "(plan - fact) / plan")
        self.assertEqual(d["result"]["substituted"], "(5000 - 4000) / 5000")
        self.assertEqual(d["inputs"]["vars"], {"plan": 5000, "fact": 4000})
        self.assertEqual(len(d["plan_hash"]), 64)
        self.assertIn("--plan-hash", d["claim_hint"])
        r = run_cli("schet", "share * groups", "--var", "share=0,3", "--var", "groups=4000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("подстановка: 0.3 * 4000", r.stdout)
        self.assertIn("итог:        1 200", r.stdout)

    def test_forbidden(self):
        for expr in ("__import__('os')", "open('x')", "eval('1')", "print(1)"):
            with self.assertRaises(calc.CalcError, msg=expr) as e:
                calc.schet(expr)
            self.assertIn("не разрешена", str(e.exception))
        for expr in ("__import__('os').system('true')", "(1).real", "().__class__.__bases__", "x.y", "'a'.upper()"):
            with self.assertRaises(calc.CalcError, msg=expr) as e:
                calc.schet(expr, {"x": 1})
            self.assertIn("атрибут", str(e.exception))
        for expr in ("'abc'", "[x for x in [1]]", "lambda: 1", "a if 1 else 2", "1 < 2", "True + 1"):
            with self.assertRaises(calc.CalcError, msg=expr):
                calc.schet(expr, {"a": 1})
        with self.assertRaises(calc.CalcError):
            calc.schet("9 ** 9999")

    def test_zero_division_and_unknown_name_are_clear(self):
        with self.assertRaises(calc.CalcError) as e:
            calc.schet("a / (b - b)", {"a": 1, "b": 2})
        self.assertIn("деление на ноль", str(e.exception))
        with self.assertRaises(calc.CalcError) as e:
            calc.schet("plan - fact", {"plan": 1})
        self.assertIn("--var fact=", str(e.exception))
        r = run_cli("schet", "1 / 0", "--json")
        self.assertEqual(r.returncode, 3)
        self.assertIn("деление на ноль", json.loads(r.stdout)["error"])

    def test_bad_vars(self):
        for bad in ("plan", "1x=2", "__x=1", "max=1", "plan=много"):
            with self.assertRaises(calc.CalcError, msg=bad):
                calc.parse_vars([bad])


if __name__ == "__main__":
    unittest.main()
