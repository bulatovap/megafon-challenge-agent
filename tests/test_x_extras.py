"""P1: вопрос-переворот (qfilter) и останов сбора по дешёвому сигналу (saturation)."""
from __future__ import annotations

import json
import unittest

from x_fixtures import XCase

from tools.bulochka import core, journal

CANDS = [
    {"question": "Какая доля промо в триалах?", "switch": "30%", "plausible": "20..45%",
     "if_above": "O3 первым", "if_below": "O2", "options": ["30% и больше", "меньше 30%"]},
    {"question": "Сколько стоит онлайн-кинотеатр у конкурента?", "switch": "600 ₽", "plausible": "299..449 ₽"},
    {"question": "Какой NPS у платящих?"},
]


class TestQfilter(XCase):
    def setUp(self):
        super().setUp()
        self.journal("plan", "2026-11-09T09:00:00Z")

    def test_only_flipping_question_asked(self):
        r, out = self.cli_json("qfilter", "--candidates", json.dumps(CANDS, ensure_ascii=False))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = out["data"]
        self.assertEqual(d["asked"], ["Какая доля промо в триалах?"])
        self.assertEqual(d["filtered"], 2)
        self.assertIn("Отфильтровано 2 — не меняли вывод", d["phrase"])
        g = core.get("gate", d["gate_ids"][0], self.slug)
        self.assertTrue(g["flips"])
        self.assertEqual(g["filtered_questions"], 2)
        self.assertEqual(g["outcomes_preview"], {"30% и больше": "O3 первым", "меньше 30%": "O2"})

    def test_dry_run_no_gate(self):
        _, out = self.cli_json("qfilter", "--candidates", json.dumps(CANDS, ensure_ascii=False), "--dry-run")
        self.assertEqual(out["data"]["gate_ids"], [])
        self.assertEqual(core.latest("gate", self.slug), {})

    def test_max_three_asked(self):
        many = [dict(CANDS[0], question=f"Вопрос {i}") for i in range(5)]
        _, out = self.cli_json("qfilter", "--candidates", json.dumps(many, ensure_ascii=False))
        self.assertEqual(len(out["data"]["asked"]), 3)
        self.assertEqual(len(out["data"]["overflow"]), 2)


class TestSaturation(XCase):
    def setUp(self):
        super().setUp()
        self.journal("research", "2026-11-09T09:00:00Z")

    def add(self, n_sources, material_for):
        for i in range(1, n_sources + 1):
            core.put("source", {"id": f"S{i:03d}", "title": "t", "status": "active"}, self.slug)
        for k, sid in enumerate(material_for, 1):
            core.put("claim", {"id": f"C{k:03d}", "text": "t", "type": "fact", "status": "active", "material": True,
                               "source_ids": [sid]}, self.slug)

    def payload(self):
        return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(self.root),
                "tool_input": {"command": "python3 -m tools.bulochka source fetch --url https://x"}}

    def test_stop_after_two_empty_batches(self):
        self.add(9, ["S001", "S002"])
        _, out = self.cli_json("saturation")
        self.assertTrue(out["data"]["stop"])
        self.assertEqual([b["new_material"] for b in out["data"]["batches"]], [2, 0, 0])

    def test_no_stop_while_new_material(self):
        self.add(9, ["S001", "S008"])
        _, out = self.cli_json("saturation")
        self.assertFalse(out["data"]["stop"])

    def test_hook_signals_once(self):
        self.add(9, ["S001"])
        r = self.run_ext("post_tool_saturation.py", self.payload())
        self.assertIn("Сбор насытился", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
        r2 = self.run_ext("post_tool_saturation.py", self.payload())
        self.assertEqual(r2.stdout.strip(), "")

    def test_hook_ignores_other_commands(self):
        self.add(9, ["S001"])
        p = self.payload()
        p["tool_input"]["command"] = "ls"
        self.assertEqual(self.run_ext("post_tool_saturation.py", p).stdout.strip(), "")


class TestFirstScreen(XCase):
    def setUp(self):
        super().setUp()
        core.put("claim", {"id": "C001", "text": "t", "type": "fact", "status": "active"}, self.slug)
        core.put("decision", {"id": "D001", "question": "q", "status": "chosen"}, self.slug)

    def answers(self, **kw):
        a = {"what": "Запускаем онбординг в триале [D001]", "why": "Причина отписок — не пользуются [C001]",
             "money": "≈75 ₽ из стоимости подписки, 8 мин продакта [D001]", "check": "A/B до 15.01 [C001]"}
        a.update(kw)
        return json.dumps(a, ensure_ascii=False)

    def test_writes_with_refs(self):
        r, out = self.cli_json("firstscreen", "D001", "--answers", self.answers(), "--sentence", "Онбординг [D001]")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.root / "projects" / self.slug / "report" / "01_first_screen.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("**"), 8)

    def test_answer_without_ref_fails(self):
        r, out = self.cli_json("firstscreen", "D001", "--answers", self.answers(why="потому что так лучше"),
                               "--sentence", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("без ссылки", out["errors"][0]["msg"])
        self.assertFalse((self.root / "projects" / self.slug / "report" / "01_first_screen.md").exists())

    def test_dangling_ref_fails(self):
        r, out = self.cli_json("firstscreen", "D001", "--answers", self.answers(check="см. C999"), "--sentence", "x")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(out["errors"][0]["ids"], ["C999"])


if __name__ == "__main__":
    unittest.main()
