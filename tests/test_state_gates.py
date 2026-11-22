"""Ворота: ask (2–8 вариантов, W08), answer с review_seconds."""
from __future__ import annotations

import unittest

from _s_env import StateCase, core, gates, journal


class TestGates(StateCase):
    def setUp(self):
        super().setUp()
        self.new()

    def test_options_count_validated(self):
        self.assertEqual(self.cli("ask", "--kind", "question", "--question", "?", "--options", "один")[0],
                         core.EXIT_USAGE)
        self.assertEqual(self.cli("ask", "--kind", "question", "--question", "?", "--options",
                                  *[str(i) for i in range(1, 10)])[0], core.EXIT_USAGE)
        code, res = self.cli("ask", "--kind", "question", "--question", "Что проверить?", "--options",
                             *[f"C00{i}" for i in range(1, 6)], "поднять бюджет", "остановиться", "--why", "бюджет")
        self.assertEqual(code, 0, res)
        self.assertEqual(len(core.get("gate", res["data"]["gate_id"])["options"]), 7)

    def test_ask_record_fields_and_warnings(self):
        code, res = self.cli("ask", "--kind", "question", "--question", "Цена чувствительна?", "--options", "да", "нет",
                             "--flips", "--claims", "C001,C002", "C003", "C004", "C005", "C006", "C007", "C008")
        self.assertEqual(code, 0)
        rules = [e["rule"] for e in res["errors"]]
        self.assertIn("W08", rules)
        g = core.get("gate", res["data"]["gate_id"])
        self.assertEqual(g["kind"], "question")
        self.assertTrue(g["flips"])
        self.assertEqual(len(g["claims_shown"]), 8)
        self.assertIsNone(g["answer"])
        self.assertEqual(g["step"], "intake")

    def test_answer_review_seconds_and_option_index(self):
        g, _, _ = gates.ask("choose_option", "Какой вариант?", ["O001 оставить", "O002 закрыть"], why="выбор")
        code, res = self.cli("answer", g["id"], "--answer", "2", "--seconds", "95")
        self.assertEqual(code, 0)
        self.assertEqual(res["data"]["review_seconds"], 95)
        rec = core.get("gate", g["id"])
        self.assertEqual((rec["answer"], rec["answered_by"]), ("O002 закрыть", "human"))
        ev = [r for r in journal.records() if r["operation"] == "gate.answer"]
        self.assertEqual(ev[-1]["review_seconds"], 95)
        # повтор того же ответа — идемпотентно, другой ответ — ошибка
        self.assertEqual(self.cli("answer", g["id"], "--answer", "O002 закрыть")[0], 0)
        self.assertEqual(self.cli("answer", g["id"], "--answer", "1")[0], core.EXIT_USAGE)

    def test_review_seconds_measured_from_shown_at(self):
        g, _, _ = gates.ask("question", "?", ["а", "б"])
        rec = gates.answer(g["id"], "а")
        self.assertIsInstance(rec["review_seconds"], int)
        self.assertGreaterEqual(rec["review_seconds"], 0)

    def test_open_human_gate_not_duplicated(self):
        a, c1, _ = gates.ask("brief_approval", "Бриф?", ["да", "нет"])
        b, c2, _ = gates.ask("brief_approval", "Бриф ещё раз?", ["да", "нет"])
        self.assertEqual((a["id"], c1, c2), (b["id"], True, False))
        c, _, _ = gates.ask("choose_option", "Какой вариант?", ["O001", "O002"])
        self.assertIn("не делаются", c["default_if_no_answer"])


if __name__ == "__main__":
    unittest.main()
