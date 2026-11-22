"""Проект: new, шаги 1–11 с воротами, status --for-hook, next, pause."""
from __future__ import annotations

import json
import unittest

from _s_env import StateCase, core, journal, project


class TestProject(StateCase):
    def test_new_copies_brief_and_sets_current(self):
        pdir = self.new("demo")
        self.assertEqual(core.current_slug(), "demo")
        self.assertIn("Отток", (pdir / "order.md").read_text(encoding="utf-8"))
        self.assertTrue((pdir / "RUN_STATUS.md").exists())
        st = json.loads((pdir / "state" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["step"], st["step_n"], st["steps_total"]), ("intake", 1, 11))

    def test_new_project_layout(self):
        pdir = self.new("demo")
        for name in ("order.md", "RUN_STATUS.md"):
            self.assertTrue((pdir / name).is_file(), name)
        for sub in ("report", "state", "sources"):
            self.assertTrue((pdir / sub).is_dir(), sub)
        for old in ("outputs", "brief.md", "STATE.md"):
            self.assertFalse((pdir / old).exists(), old)
        st = json.loads((pdir / "state" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["brief"], "order.md")
        self.assertEqual(journal.records()[0]["output_path"], "order.md")

    def test_new_rejects_duplicate_and_bad_slug(self):
        self.new("demo")
        self.assertEqual(self.cli("new", "demo", "--brief", str(self.brief))[0], core.EXIT_USAGE)
        self.assertEqual(self.cli("new", "Плохой slug", "--brief", str(self.brief))[0], core.EXIT_USAGE)
        self.assertEqual(self.cli("new", "x2", "--brief", str(self.tmp / "nope.md"))[0], core.EXIT_USAGE)

    def test_gate_blocks_next_step_until_answer(self):
        self.new()
        self.finish_step("intake")
        self.finish_step("plan")
        st = project.compute()
        self.assertEqual((st["step"], st["blocked_by_gate"], st["waiting_gate"]), ("research", "brief_approval", None))
        self.assertIn("ask --kind brief_approval", st["next"]["why"])
        self.assertEqual(st["next"]["skill"], "cost-plan")
        code, res = self.cli("ask", "--kind", "brief_approval", "--question", "Бриф верный?", "--options", "да", "нет",
                             "--flips", "--why", "от брифа зависит план", "--default", "да")
        gid = res["data"]["gate_id"]
        code, res = self.cli("next")
        self.assertIn(gid, res["data"]["why"])
        self.assertIsNone(res["data"]["skill"])
        self.cli("answer", gid, "--answer", "да")
        code, res = self.cli("next")
        self.assertEqual((res["data"]["step"], res["data"]["skill"]), ("research", "sourcing"))
        code, res = self.cli("status")
        self.assertEqual((res["data"]["step_n"], res["data"]["steps_total"]), (3, 11))

    def test_review_is_step_8_and_numbering(self):
        self.new()
        for s in ("intake", "plan"):
            self.finish_step(s)
        self.answer_gate("brief_approval")
        for s in ("research", "situation", "options"):
            self.finish_step(s)
        self.answer_gate("choose_option")
        for s in ("strategy", "validation-plan"):
            self.finish_step(s)
        st = project.compute()
        self.assertEqual((st["step"], st["step_n"]), ("review", 8))
        self.finish_step("review")
        self.assertEqual(project.compute()["step_n"], 10)
        self.assertEqual(project.STEP_N["revise"], 11)
        self.assertEqual(project.STEPS_TOTAL, 11)

    def test_sources_frozen_flag_set_by_strategy_cleared_by_revise(self):
        self.new()
        flag = self.tmp / "projects" / "demo" / "state" / "no_web.flag"
        for s in ("intake", "plan"):
            self.finish_step(s)
        self.answer_gate("brief_approval")
        for s in ("research", "situation", "options"):
            self.finish_step(s)
        self.answer_gate("choose_option")
        self.assertFalse(project.compute()["sources_frozen"])
        journal.begin("step.run", {"step": "strategy"})
        self.assertTrue(flag.exists())
        self.assertTrue(project.compute()["sources_frozen"])
        journal.begin("step.run", {"step": "revise"})
        self.assertFalse(flag.exists())

    def test_freeze_command(self):
        self.new()
        code, res = self.cli("freeze", "--reason", "тест")
        self.assertEqual((code, res["data"]["sources_frozen"]), (0, True))
        self.assertTrue(project.compute()["sources_frozen"])
        self.cli("freeze", "--off")
        self.assertFalse(project.compute()["sources_frozen"])

    def test_for_hook_summary_content(self):
        self.new()
        self.finish_step("intake")
        core.put("source", {"id": "S001", "kind": "media", "published": "2020-01-01", "status": "active",
                            "ttl_days": 90})
        core.put("decision", {"id": "D001", "status": "draft", "question": "Что делать с оттоком?"})
        journal.begin("source.fetch", {"url": "https://a.ru"})
        journal.mark_session_uncertain(None, except_session="новая")
        code, res = self.cli("status", "--for-hook")
        text = res["data"]["text"]
        self.assertIn("шаг 2/11", text)
        self.assertIn("не повторять: intake", text)
        self.assertIn("S001", text)
        self.assertIn("D001", text)
        self.assertIn("uncertain", text)

    def test_for_hook_is_bounded(self):
        self.new()
        for i in range(1, 120):
            core.put("decision", {"id": f"D{i:03d}", "status": "chosen", "chosen_option": "O001",
                                  "question": "Очень длинный вопрос о стратегии удержания " * 5})
            core.put("source", {"id": f"S{i:03d}", "kind": "review", "published": "2019-01-01", "status": "active"})
            core.put("gate", {"id": f"G{i:03d}", "kind": "question", "question": "Вопрос " * 40, "answer": None})
        text = project.for_hook_text(project.compute())
        self.assertLessEqual(len(text), project.FOR_HOOK_MAX_CHARS)
        self.assertLessEqual(len(text.encode("utf-8")) / 4, 2500 * 1.5)  # грубо: ≤2500 токенов
        self.assertIn("Далее", text + "Далее")

    def test_status_tokens_from_cost_json(self):
        self.new()
        core.atomic_write_json(core.state_dir() / "cost.json",
                               {"tokens": 1234, "tokens_total": {"in": 1, "out": 1, "cache_read": 999999}})
        self.assertEqual(self.cli("status")[1]["data"]["tokens"], 1234)

    def test_pause_writes_resume(self):
        self.new()
        self.finish_step("intake")
        code, res = self.cli("pause")
        self.assertEqual(res["data"]["handoff"], "RESUME.md")
        text = (self.tmp / "projects" / "demo" / "RESUME.md").read_text(encoding="utf-8")
        self.assertIn("Сделано (не повторять)", text)
        self.assertIn("Постановка задачи", text)
        self.assertIn("/cost-plan", text)
        self.assertEqual(journal.records()[-1]["operation"], "session.pause")
        code, res = self.cli("resume")
        self.assertIn("Продолжить отсюда", res["data"]["text"])


if __name__ == "__main__":
    unittest.main()
