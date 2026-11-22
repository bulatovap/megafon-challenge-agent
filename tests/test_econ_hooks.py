"""Тесты хуков statusline.py и budget.py через stdin, как их вызывает Claude Code."""
from __future__ import annotations

import json
import unittest

from cost_fixtures import EconCase

from tools.bulochka import core, cost


class TestStatusline(EconCase):
    def test_line_with_project(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        core.put("claim", {"text": "a", "type": "fact", "status": "active", "number_on_page": "pass"}, self.slug)
        core.put("claim", {"text": "b", "type": "assumption", "status": "active", "material": True,
                           "decision_link": ["O001"]}, self.slug)
        # долг считается по вариантам (decisions.total_evidence_debt), как в `bulochka status`
        core.put("option", {"decision_id": "D001", "title": "o", "kind": "keep", "status": "open"}, self.slug)
        core.put("objection", {"target_id": "O001", "text": "r", "status": "open"}, self.slug)
        r = self.run_hook("statusline.py", self.statusline_input(pct=45, ctx=38))
        self.assertEqual(r.returncode, 0, r.stderr)
        line = r.stdout.strip()
        self.assertLessEqual(len(line), 100, line)
        self.assertTrue(line.startswith("этап 3/11 | "), line)
        self.assertIn("45% нед.", line)  # один замер — текущий %, без «+»
        self.assertIn("утв 2 (1✓)", line)
        self.assertIn("долг 1", line)
        self.assertIn("возр 1", line)
        self.assertIn("1k ток", line)
        self.assertIn("ctx 38%", line)
        saved = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        self.assertEqual(saved["writer"], "statusline")
        self.assertEqual(saved["weekly_limit_pct_before"], 45)

    def test_week_delta_shown_after_second_sample(self):
        self.run_hook("statusline.py", self.statusline_input(pct=45))
        r = self.run_hook("statusline.py", self.statusline_input(pct=48))
        self.assertIn("+3% нед.", r.stdout)

    def test_no_project_short_line(self):
        (self.root / "projects" / "CURRENT").unlink()
        r = self.run_hook("statusline.py", self.statusline_input(pct=45, ctx=10))
        self.assertEqual(r.returncode, 0)
        self.assertIn("сметы ещё нет", r.stdout)
        self.assertIn("ctx 10%", r.stdout)
        self.assertEqual(r.stderr, "")

    def test_garbage_stdin_does_not_crash(self):
        for payload in ("", "не json", "[1,2]"):
            r = self.run_hook("statusline.py", payload)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(r.stdout.strip())
            self.assertNotIn("Traceback", r.stderr)


class TestBudget(EconCase):
    def payload(self):
        return {"session_id": "s", "transcript_path": self.transcript, "hook_event_name": "UserPromptSubmit",
                "prompt": "продолжай", "cwd": str(self.root)}

    def test_over_80_percent_refuses_once(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        core.put("claim", {"text": "b", "type": "assumption", "status": "active", "material": True}, self.slug)
        cost.compute(self.slug, transcripts=[self.transcript])
        d = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        d["step_budgets"] = {"research": 1500}  # 1262 > 0.8 × 1500
        core.atomic_write_json(cost.cost_path(self.slug), d)
        r = self.run_hook("budget.py", self.payload())
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BUDGET", ctx)
        self.assertIn("84%", ctx)
        self.assertIn("C001", ctx)
        refs = list(core.latest("refusal", self.slug).values())
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["tag"], "BUDGET")
        self.assertEqual(refs[0]["actor"], "hook")
        self.assertEqual(refs[0]["unverified_ids"], ["C001"])
        r2 = self.run_hook("budget.py", self.payload())
        self.assertIn("BUDGET", r2.stdout)
        self.assertEqual(len(core.latest("refusal", self.slug)), 1)

    def test_under_threshold_silent(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        r = self.run_hook("budget.py", self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(core.latest("refusal", self.slug), {})

    def test_internal_error_goes_to_incidents(self):
        (self.root / "config" / "pricing.json").write_text("{битый", encoding="utf-8")
        cost_json = cost.cost_path(self.slug)
        cost_json.write_text('{"step_budgets": {"x": "не число"}}', encoding="utf-8")
        self.journal("x", "2026-11-09T09:00:00Z")
        r = self.run_hook("budget.py", self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        inc = core.read_jsonl(core.state_dir(self.slug) / "incidents.jsonl")
        self.assertEqual(inc[-1]["hook"], "budget")


if __name__ == "__main__":
    unittest.main()
