"""Экономика кодом (живой прогон 21.11): смета до плана, тест на разворот до расчёта, ценник после фиксации."""
from __future__ import annotations

import json
import unittest

from x_fixtures import XCase

from tools.bulochka import core, journal


class TestEconGuard(XCase):
    def pre(self, cmd):
        r = self.run_ext("econ_guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                           "tool_input": {"command": cmd}})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else {}

    def test_plan_needs_smeta(self):
        self.research_project()
        out = self.pre("python3 -m tools.bulochka journal begin --step plan")
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("smeta", out["permissionDecisionReason"])
        smeta = core.project_dir() / "report" / "00_smeta.md"
        smeta.parent.mkdir(parents=True, exist_ok=True)
        smeta.write_text("смета", encoding="utf-8")
        self.assertEqual(self.pre("python3 -m tools.bulochka journal begin --step plan"), {})

    def test_calc_needs_fresh_flip(self):
        self.research_project()
        out = self.pre("python3 scripts/calc.py mde --base 0.12 --mde 0.03 --json")
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("flip", out["permissionDecisionReason"])
        journal.event("flip.run", actor="agent", question="размер пилота")
        self.assertEqual(self.pre("python3 scripts/calc.py mde --base 0.12 --mde 0.03 --json"), {})
        self.assertEqual(self.pre("python3 scripts/calc.py --help"), {})

    def test_schet_needs_no_flip(self):
        self.research_project()  # flip ни разу не было: арифметика брифа — не расчёт-исследование
        self.assertEqual(self.pre('python3 scripts/calc.py schet "(plan - fact) / plan" --var plan=5000 '
                                  '--var fact=4000 --json'), {})

    def test_own_calc_scripts_and_flip_of_other_step(self):
        self.research_project()
        out = self.pre(f"python3 projects/{core.current_slug()}/calc/sizing.py")
        self.assertEqual(out.get("permissionDecision"), "deny")
        self.assertIn("calc.py schet", out["permissionDecisionReason"])  # куда положить арифметику
        journal.event("flip.run", actor="agent", step="plan", question="другой вопрос")
        out = self.pre("python3 scripts/calc.py mde --json")
        self.assertEqual(out.get("permissionDecision"), "deny")  # flip был на другом шаге
