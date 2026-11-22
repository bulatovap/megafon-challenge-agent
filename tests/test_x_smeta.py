"""Смета до запуска: по истории прогонов, разбивка, диапазон при большой ошибке, close без дублей, leave-one-out."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

from x_fixtures import PRODUCT_ROOT, XCase

from tools.bulochka import core, cost, x_econ, x_smeta

BRIEF = "# B05. Бандл\n\nТип задачи: монетизация · Шаги: 1–7, акцент на деньгах\n\nТекст брифа.\n"


def run(i, tokens, est=None, err=None, weekly=None, steps=None):
    steps = steps or ["intake", "plan", "research", "situation", "options", "strategy", "validation-plan"]
    per = tokens // len(steps)
    return {"run_id": f"r{i}", "brief_id": f"B0{i}", "tokens": per * len(steps),
            "tokens_by_step": {s: per for s in steps}, "steps": steps, "questions": 5,
            "review_seconds_median": 60, "verify_minutes": 6, "weekly_delta": weekly,
            "estimate_tokens": est, "error_pct": err}


class TestSmeta(XCase):
    def setUp(self):
        super().setUp()
        self.brief = self.root / "B05.md"
        self.brief.write_text(BRIEF, encoding="utf-8")

    def seed(self, rows):
        for r in rows:
            x_econ.append_history(r)

    def test_no_history_core_values(self):
        r, out = self.cli_json("smeta", "--brief", str(self.brief))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = out["data"]
        self.assertIsNone(d["weekly_limit_pct"])
        self.assertEqual(set(d["breakdown"]), {"model", "search", "human"})
        self.assertEqual(d["steps"], ["intake", "plan", "research", "situation", "options", "strategy",
                                      "validation-plan"])
        self.assertTrue(d["gate_id"].startswith("G"))
        g = core.get("gate", d["gate_id"], self.slug)
        self.assertEqual(g["options"], ["идём", "урезать", "отмена"])
        self.assertEqual(self.cost()["estimate"]["tokens"], d["tokens"])
        card = (self.root / "projects" / self.slug / "report" / "00_smeta.md").read_text(encoding="utf-8")
        self.assertIn("из стоимости подписки", card)
        self.assertIn("замеров нет", card)

    def test_history_medians_and_weekly(self):
        self.seed([run(1, 700000, weekly=1.4), run(2, 700000, weekly=1.4), run(3, 1400000, weekly=2.8)])
        _, out = self.cli_json("smeta", "--brief", str(self.brief))
        d = out["data"]
        self.assertEqual(d["tokens"], 700000)
        self.assertAlmostEqual(d["weekly_limit_pct"], 1.4, places=2)
        self.assertEqual(d["rub_equiv"], cost.money_from_pct(1.4)[1])
        self.assertEqual(d["minutes_human"], 3 + 6)  # 3 ворот × 60 с + 6 мин проверки
        self.assertEqual(d["breakdown"]["search"], 100000)
        self.assertIsNone(d["range"])

    def test_range_when_error_big(self):
        self.seed([run(1, 700000, est=300000, err=133.3), run(2, 900000, est=400000, err=125.0),
                   run(3, 800000, est=500000, err=60.0)])
        _, out = self.cli_json("smeta", "--brief", str(self.brief))
        d = out["data"]
        self.assertIsNotNone(d["range"])
        self.assertLess(d["range"][0], d["range"][1])
        card = (self.root / "projects" / self.slug / "report" / "00_smeta.md").read_text(encoding="utf-8")
        self.assertIn("диапазон", card)

    def test_manual_weekly_range(self):
        r = run(1, 900000)
        r.update({"weekly_delta_range": [1, 2], "weekly_delta_source": "замер человека"})
        self.seed([r])
        _, out = self.cli_json("smeta", "--brief", str(self.brief))
        d = out["data"]
        self.assertIsNone(d["weekly_limit_pct"])
        self.assertEqual(d["weekly_limit_pct_range"], [1.0, 2.0])

    def test_gate_not_duplicated(self):
        _, a = self.cli_json("smeta", "--brief", str(self.brief))
        _, b = self.cli_json("smeta", "--brief", str(self.brief))
        self.assertEqual(a["data"]["gate_id"], b["data"]["gate_id"])

    def test_close_appends_once(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        self.cli_json("smeta", "--brief", str(self.brief))
        cost.compute(self.slug, transcripts=[self.transcript])
        _, out = self.cli_json("smeta", "close")
        self.assertTrue(out["data"]["added"])
        row = out["data"]["row"]
        self.assertIsNotNone(row["error_pct"])
        self.assertEqual(row["brief_id"], "B05")
        _, out2 = self.cli_json("smeta", "close")
        self.assertFalse(out2["data"]["added"])
        self.assertEqual(len(core.read_jsonl(x_econ.history_path())), 1)

    def test_steps_of_brief(self):
        self.assertEqual(len(x_econ.steps_of_brief("Шаги: все 11")), 10)
        self.assertEqual(x_econ.steps_of_brief("Шаги: 1–7 (+10)")[-1], "decide")
        self.assertEqual(x_econ.steps_of_brief("Шаги: 1–3, акцент"), ["intake", "plan", "research"])

    def test_leave_one_out_eval(self):
        self.seed([run(1, 700000), run(2, 770000), run(3, 840000)])
        r = subprocess.run([sys.executable, str(PRODUCT_ROOT / "scripts" / "smeta_eval.py")], capture_output=True,
                           text=True, cwd=str(PRODUCT_ROOT), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / "projects" / self.slug / "report" / "smeta_eval.md").exists())
        self.assertFalse((self.root / "outputs").exists())

    def test_eval_too_few_runs(self):
        self.seed([run(1, 700000)])
        r = subprocess.run([sys.executable, str(PRODUCT_ROOT / "scripts" / "smeta_eval.py")], capture_output=True,
                           text=True, cwd=str(PRODUCT_ROOT), timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Мало прогонов", r.stdout)

    # --- режим сбора и пересчёт (прогон 22.11: «только бриф», а в смете 188k на поиск) ---

    def card_text(self):
        return (self.root / "projects" / self.slug / "report" / "00_smeta.md").read_text(encoding="utf-8")

    def test_no_web_smeta_has_no_search_line(self):
        self.seed([run(1, 700000), run(2, 700000)])
        _, web = self.cli_json("smeta", "--brief", str(self.brief))
        self.assertEqual(web["data"]["collect"], "web")
        self.assertIn("- Поиск (", self.card_text())
        _, out = self.cli_json("smeta", "--brief", str(self.brief), "--no-web")
        d = out["data"]
        self.assertEqual(d["collect"], "brief")
        self.assertEqual(d["breakdown"]["search"], 0)
        self.assertLess(d["tokens"], web["data"]["tokens"])
        card = self.card_text()
        self.assertNotIn("Поиск", card)
        self.assertIn("только бриф", card)

    def test_brief_only_from_product_answer(self):
        g = core.put("gate", {"step": "intake", "kind": "question", "question": "Какие данные есть?",
                              "options": ["Только бриф", "Бриф и поиск"], "answer": "Только бриф",
                              "answer_raw": "1. Данных сверх брифа нет", "answered_by": "human"}, self.slug)
        _, out = self.cli_json("smeta", "--brief", str(self.brief))
        self.assertEqual(out["data"]["collect"], "brief")
        self.assertIn(g["id"], out["data"]["collect_basis"])
        self.brief.write_text(BRIEF + "\nСбор: бриф и открытые источники\n", encoding="utf-8")
        _, out = self.cli_json("smeta", "--brief", str(self.brief))
        self.assertEqual(out["data"]["collect"], "web")  # строка брифа сильнее старого ответа

    def test_brief_only_research_from_history_without_search(self):
        web = run(1, 700000)
        brief = run(2, 560000)
        brief["tokens_by_step"]["research"] = 20000
        brief["collect"] = "brief"
        self.seed([web, brief])
        est = x_smeta.estimate(BRIEF, collect="brief")
        self.assertEqual(est["tokens_by_step"]["research"], 20000)
        est = x_smeta.estimate(BRIEF, collect="web", questions=5)
        self.assertEqual(est["tokens_by_step"]["research"], 100000)  # 5 вопросов × 20k по прогону с поиском

    def test_recalc_small_change_keeps_gate_closed(self):
        self.seed([run(1, 700000), run(2, 700000)])
        _, a = self.cli_json("smeta", "--brief", str(self.brief))
        made = self.cost()["estimate"]["made_at"]
        plan = self.root / "projects" / self.slug / "cost_plan.md"
        plan.write_text("Q1 Q2 Q3 Q4 Q5 Q6\n", encoding="utf-8")  # 6 вопросов вместо 5: +20k, < 15%
        r, out = self.cli_json("smeta", "recalc")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = out["data"]
        self.assertFalse(d["gate_needed"])
        self.assertIsNone(d["gate_id"])
        self.assertEqual(d["questions"], 6)
        est = self.cost()["estimate"]
        self.assertEqual(est["made_at"], made)  # id прогона в истории не меняется
        self.assertEqual(est["previous_tokens"], a["data"]["tokens"])
        self.assertIn("Пересчёт после плана", self.card_text())
        opened = [g for g in core.latest("gate", self.slug).values() if g.get("question") == x_smeta.RECALC_QUESTION]
        self.assertEqual(opened, [])

    def test_recalc_big_change_asks_again(self):
        rows = [run(1, 700000), run(2, 700000)]
        for r in rows:  # поиск — четверть прогона, как в B01
            r["tokens_by_step"]["research"] = 250000
        self.seed(rows)
        self.cli_json("smeta", "--brief", str(self.brief))
        _, out = self.cli_json("smeta", "recalc", "--no-web")
        d = out["data"]
        self.assertEqual(d["collect"], "brief")
        self.assertTrue(d["gate_needed"], d)
        self.assertLess(d["change_pct"], -15)
        g = core.get("gate", d["gate_id"], self.slug)
        self.assertEqual((g["question"], g["options"]), (x_smeta.RECALC_QUESTION, ["идём", "урезать", "отмена"]))
        self.assertNotIn("Поиск", self.card_text())
        _, again = self.cli_json("smeta", "recalc", "--no-web")
        self.assertEqual(again["data"]["gate_id"], d["gate_id"])  # те же ворота, пока не отвечены

    def test_recalc_threshold_from_pricing(self):
        self.seed([run(1, 700000), run(2, 700000)])
        self.cli_json("smeta", "--brief", str(self.brief))
        cfg = self.root / "config" / "pricing.json"
        import json as _json
        data = _json.loads(cfg.read_text(encoding="utf-8"))
        data["smeta_recalc_gate_pct"] = 90
        cfg.write_text(_json.dumps(data), encoding="utf-8")
        _, out = self.cli_json("smeta", "recalc", "--no-web")
        self.assertEqual(out["data"]["threshold_pct"], 90)
        self.assertFalse(out["data"]["gate_needed"])

    def test_recalc_without_smeta_is_usage_error(self):
        r = self.run_cli("smeta", "recalc")
        self.assertEqual(r.returncode, 3)

    def test_close_records_collect(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        self.cli_json("smeta", "--brief", str(self.brief), "--no-web")
        cost.compute(self.slug, transcripts=[self.transcript])
        _, out = self.cli_json("smeta", "close")
        self.assertEqual(out["data"]["row"]["collect"], "brief")

    def test_eval_uses_collect_of_each_run(self):
        from importlib import util
        spec = util.spec_from_file_location("smeta_eval", PRODUCT_ROOT / "scripts" / "smeta_eval.py")
        ev = util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        web = run(1, 700000)
        brief = run(2, 600000)
        brief["tokens_by_step"]["research"] = 20000
        brief["sources"] = 1
        res = {x["run_id"]: x for x in ev.evaluate([web, brief])}
        self.assertEqual(res["r2"]["collect"], "brief")
        self.assertEqual(res["r1"]["collect"], "web")
        # без поиска смета прогона r2 не несёт шага сбора с поиском r1 (100k)
        self.assertLess(res["r2"]["estimate"], 700000 - 50000)


if __name__ == "__main__":
    unittest.main()
