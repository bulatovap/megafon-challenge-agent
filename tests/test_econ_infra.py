"""Тесты doctor, export --one-pager и scripts/metrics.py (корень — папка с кириллицей и пробелом)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from cost_fixtures import PRODUCT_ROOT, EconCase

from tools.bulochka import core, cost, doctor, export


class DoctorCase(EconCase):
    def setUp(self):
        super().setUp()
        hooks = self.root / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy(PRODUCT_ROOT / ".claude" / "settings.json", self.root / ".claude" / "settings.json")
        for h in doctor.HOOK_FILES:
            (hooks / h).write_text("", encoding="utf-8")


class TestDoctor(DoctorCase):
    def test_green_in_cyrillic_path(self):
        self.journal("intake", core.now_iso(), actor="hook", operation="session.start")
        checks = {c["check"]: c for c in doctor.run_checks(with_claude=False)}
        self.assertEqual([c for c in checks.values() if c["status"] == "fail"], [])
        self.assertTrue(checks["path"]["non_ascii"])
        self.assertTrue(checks["path"]["spaces"])
        self.assertEqual(checks["hooks_fired"]["status"], "ok")
        r = self.run_cli("doctor", "--no-claude")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Готово к смете", r.stdout)

    def test_no_hook_traces_is_warning(self):
        c = doctor.check_hooks_fired(self.slug)
        self.assertEqual(c["status"], "warn")
        self.assertIn("доверие", c["msg"])

    def test_corrupt_jsonl_and_committed_without_file_fail(self):
        sd = core.state_dir(self.slug)
        with (sd / "claims.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"id":"C001"}\n{оборванная строка\n')
        (self.root / "projects" / self.slug / "report" / "20_baseline.md").write_text("ok", encoding="utf-8")
        self.journal("situation", core.now_iso(), key="k1", status="committed", output_path="report/20_baseline.md")
        self.journal("options", core.now_iso(), key="k2", status="committed", output_path="report/30_alternatives.md")
        checks = {c["check"]: c for c in doctor.run_checks(with_claude=False)}
        self.assertEqual(checks["jsonl"]["status"], "fail")
        self.assertIn("claims.jsonl:2", checks["jsonl"]["msg"])
        self.assertEqual(checks["journal_files"]["status"], "fail")
        self.assertIn("30_alternatives.md", checks["journal_files"]["msg"])
        self.assertNotIn("20_baseline.md", checks["journal_files"]["msg"])
        r = self.run_cli("--json", "doctor", "--no-claude")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_missing_settings_fails(self):
        (self.root / ".claude" / "settings.json").unlink()
        self.assertEqual(doctor.check_settings()["status"], "fail")


class TestExport(EconCase):
    def seed(self):
        core.put("source", {"url": "https://example.ru", "title": "t", "status": "active"}, self.slug)
        core.put("claim", {"text": "Отток после триала 41%", "type": "fact", "status": "active",
                           "source_ids": ["S001"], "grade": "B"}, self.slug)
        core.put("decision", {"question": "Что делать с удержанием?", "status": "chosen", "chosen_option": "O002",
                              "human_gate_id": "G001", "owner": "продакт", "metric": "retention_m2",
                              "premises": [{"claim_id": "C001", "kind": "fact"}],
                              "revise_if": [{"indicator": "цена конкурента", "threshold": ">499 ₽",
                                             "check_by": "2027-02-01"}]}, self.slug)
        for t, k in (("Оставить", "keep"), ("Скидка", "change"), ("Закрыть", "close")):
            core.put("option", {"decision_id": "D001", "title": t, "kind": k, "status": "open"}, self.slug)
        core.put("objection", {"target_id": "O002", "text": "r", "status": "open"}, self.slug)

    def test_one_pager_from_registries(self):
        self.seed()
        cost.record_limit_sample(45, 1, None, self.slug)
        cost.record_limit_sample(47, 1, None, self.slug)
        cost.compute(self.slug, transcripts=[self.transcript])
        r = self.run_cli("--json", "export", "--one-pager")
        self.assertEqual(r.returncode, 0, r.stderr)
        path = json.loads(r.stdout)["data"]["paths"][0]
        from pathlib import Path
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("D001. Что делать с удержанием?", text)
        self.assertIn("| O001 Оставить | оставить |", text)
        self.assertIn("[Факт] Отток после триала 41% [C001; S001; грейд B]", text)
        self.assertIn("цена конкурента >499 ₽ — проверить к 2027-02-01", text)
        self.assertIn("Возражений открыто: 1", text)
        self.assertIn("+2 п.п.", text)
        self.assertIn("≈75 ₽", text)

    def test_export_requires_mode(self):
        r = self.run_cli("export")
        self.assertEqual(r.returncode, 3)

    def test_pdf_md_joins_report(self):
        out = self.root / "projects" / self.slug / "report"
        (out / "20_baseline.md").write_text("# Картина\n", encoding="utf-8")
        (out / "30_alternatives.md").write_text("# Варианты\n", encoding="utf-8")
        text = export.build_pdf_md(self.slug)
        self.assertLess(text.index("Картина"), text.index("Варианты"))


class TestMetrics(EconCase):
    def test_metrics_from_files(self):
        core.put("claim", {"text": "a", "type": "fact", "status": "active"}, self.slug)
        core.put("refusal", {"tag": "NO_SOURCE", "outcome": "pending"}, self.slug)
        cost.compute(self.slug, transcripts=[self.transcript])
        r = subprocess.run([sys.executable, str(PRODUCT_ROOT / "scripts" / "metrics.py")], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)
        self.assertEqual(m["project"]["claims_active"], 1)
        self.assertEqual(m["project"]["refusals_by_tag"], {"NO_SOURCE": 1})
        self.assertEqual(m["project"]["tokens"], 1262)
        self.assertIn("cost", m["repo"]["cli_command_names"])


if __name__ == "__main__":
    unittest.main()
