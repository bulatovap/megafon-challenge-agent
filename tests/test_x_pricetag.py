"""Ценник решения: минуты продакта (ворота + проверка), порядок строк, рубли из подписки, без замеров — честно."""
from __future__ import annotations

import unittest

from x_fixtures import XCase

from tools.bulochka import core


class TestPricetag(XCase):
    def fixture(self, samples=True):
        self.journal("research", "2026-11-09T09:00:00Z")
        for i in range(1, 4):
            core.put("claim", {"id": f"C00{i}", "text": "t", "type": "fact", "status": "active", "material": True,
                               "verify_minutes": 2}, self.slug)
        core.put("claim", {"id": "C004", "text": "t", "type": "fact", "status": "active", "verify_minutes": 5},
                 self.slug)  # не показано продакту — не в минутах
        core.put("gate", {"id": "G001", "kind": "brief_approval", "question": "q", "options": ["да", "нет"],
                          "claims_shown": ["C001", "C002"], "answer": "да", "review_seconds": 60}, self.slug)
        core.put("gate", {"id": "G002", "kind": "choose_option", "question": "q", "options": ["O1", "O2"],
                          "claims_shown": ["C002", "C003"], "answer": "O1", "review_seconds": 60}, self.slug)
        core.put("gate", {"id": "G003", "kind": "question", "question": "q", "options": ["a", "b"],
                          "claims_shown": ["C004"], "answer": None}, self.slug)
        core.put("decision", {"id": "D001", "question": "что делать", "status": "chosen"}, self.slug)
        if samples:
            self.run_hook("statusline.py", self.statusline_input(pct=45))
            self.run_hook("statusline.py", self.statusline_input(pct=47))

    def test_minutes_human(self):
        self.fixture()
        r, out = self.cli_json("pricetag", "D001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["data"]["minutes_human"], 8)
        self.assertEqual(out["data"]["claims_checked"], ["C001", "C002", "C003"])

    def test_line_order(self):
        self.fixture()
        self.cli_json("pricetag", "D001")
        text = (self.root / "projects" / self.slug / "report" / "90_price_tag.md").read_text(encoding="utf-8")
        rows = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertTrue(rows[0].startswith("- Токены:"))
        self.assertTrue(rows[1].startswith("- Недельный лимит: +2%"))
        self.assertTrue(rows[2].startswith("- Минуты продакта: 8"))
        self.assertIn("₽ из стоимости подписки", rows[3])
        self.assertIn("≈75 ₽", rows[3])  # 200 × 12/52 × 2% × 81

    def test_no_samples_no_rubles(self):
        self.fixture(samples=False)
        _, out = self.cli_json("pricetag")
        text = (self.root / "projects" / self.slug / "report" / "90_price_tag.md").read_text(encoding="utf-8")
        self.assertIn("Недельный лимит: замеров нет", text)
        self.assertIn("замеров нет — рубли не считаю", text)
        self.assertNotRegex(text, r"≈\d+ ₽")
        self.assertIsNone(out["data"]["rub_equiv"])

    def test_decision_fields(self):
        self.fixture()
        self.cli_json("pricetag", "D001")
        d = core.get("decision", "D001", self.slug)
        self.assertEqual(d["minutes_human"], 8)
        self.assertEqual(d["actor"], "agent")
        self.assertIn("tokens_total", d)
        self.assertEqual(d["status"], "chosen")

    def test_estimate_vs_fact_and_flip_savings(self):
        self.fixture()
        c = self.cost()
        c["estimate"] = {"tokens": 1000}
        core.atomic_write_json(self.root / "projects" / self.slug / "state" / "cost.json", c)
        self.run_cli("flip", "--switch", "12 п.п.", "--plausible", "0..3 п.п.")
        _, out = self.cli_json("pricetag")
        text = (self.root / "projects" / self.slug / "report" / "90_price_tag.md").read_text(encoding="utf-8")
        self.assertIn("Смета против факта: 1k", text)
        self.assertIn("ошибка прогноза", text)
        self.assertIn("пропущено расчётов 1", text)

    def test_unknown_decision(self):
        self.fixture()
        r = self.run_cli("pricetag", "D999")
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()
