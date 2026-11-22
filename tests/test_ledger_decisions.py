"""option / decision: HUMAN_DECISION, выбор по воротам, lock и E19, reject/revive, due, diff."""
from __future__ import annotations

import json
import unittest

from _ledger_env import LedgerCase, core

REVISE = json.dumps([{"indicator": "цена Яндекс Плюс", "threshold": ">499 ₽", "source": "S001",
                      "check_by": "2027-02-01", "assumption_id": None}], ensure_ascii=False)


class DecisionTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.did = self.add_decision()
        self.keep = self.add_option(self.did, "Оставить как есть", "keep")
        self.close = self.add_option(self.did, "Закрыть пробный период", "close")
        self.change = self.add_option(self.did, "Семейная подписка", "change", "audience")

    def test_choose_without_gate_is_human_decision(self):
        code, p = self.run_cli("decision", "choose", self.did, "--option", self.change)
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "HUMAN_DECISION")
        self.assertIn(f"Рекомендую {self.change}", p["refusal"]["phrase"])
        self.assertEqual(core.get("decision", self.did)["status"], "draft")
        self.assertEqual(self.refusals()[0]["reason"], "no_gate")

    def test_choose_by_agent_actor_refused_even_with_gate(self):
        gid = self.answered_gate(f"Берём {self.change}")
        code, p = self.run_cli("decision", "choose", self.did, "--option", self.change, "--gate", gid,
                               "--actor", "agent")
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "HUMAN_DECISION")

    def test_choose_with_unanswered_or_mismatched_gate(self):
        g = core.put("gate", {"step": "decide", "kind": "choose_option", "question": "?", "answer": None,
                              "answered_by": None})
        code, _ = self.run_cli("decision", "choose", self.did, "--option", self.change, "--gate", g["id"])
        self.assertEqual(code, 2)
        gid = self.answered_gate(f"Берём {self.keep}")
        code, _ = self.run_cli("decision", "choose", self.did, "--option", self.change, "--gate", gid)
        self.assertEqual(code, 2)
        self.assertEqual([r["reason"] for r in sorted(self.refusals(), key=lambda r: r["id"])],
                         ["gate_unanswered", "answer_mismatch"])

    def test_lock_accepts_natural_approval(self):
        """«утверждаю», «утверждаем» — согласие на воротах 3, «не утверждаю» — нет (прогон 22.11: «утверждаю с долгом» не прошёл)."""
        gid = self.answered_gate(self.change)
        self.ok("decision", "choose", self.did, "--option", self.change, "--gate", gid, "--revise-if", REVISE)
        self.reject_others(self.did)
        no = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?",
                               "answer": "Нет, пока не утверждаю", "answered_by": "human"})
        code, p = self.run_cli("decision", "lock", self.did, "--gate", no["id"])
        self.assertEqual((code, p["refusal"]["tag"]), (2, "HUMAN_DECISION"))
        yes = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?",
                                "answer": "В целом согласен, утверждаю.", "answered_by": "human"})
        self.ok("decision", "lock", self.did, "--gate", yes["id"])
        self.assertEqual(core.get("decision", self.did)["status"], "locked")

    def test_choose_with_human_gate_then_lock_blocks_edits(self):
        gid = self.answered_gate("Семейная подписка")  # по названию варианта
        data = self.ok("decision", "choose", self.did, "--option", self.change, "--gate", gid, "--revise-if", REVISE,
                       "--forecast", '{"metric":"retention_m2","p_range":[0.3,0.5],"horizon":"90d"}')
        self.assertEqual(data["chosen_option"], self.change)
        d = core.get("decision", self.did)
        self.assertEqual((d["status"], d["chosen_by"], d["human_gate_id"]), ("chosen", "human", gid))
        self.assertEqual(d["revise_if"][0]["check_by"], "2027-02-01")
        code, p = self.run_cli("decision", "lock", self.did)  # без ворот 3 — отказ
        self.assertEqual((code, p["refusal"]["tag"]), (2, "HUMAN_DECISION"))
        g3 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?",
                               "answer": "Утвердить", "answered_by": "human"})
        code, p = self.run_cli("decision", "lock", self.did, "--gate", g3["id"])  # альтернативы открыты — нет
        self.assertEqual((code, p["errors"][0]["rule"]), (1, "W14"))
        self.assertEqual(core.get("decision", self.did)["status"], "chosen")
        self.reject_others(self.did)
        self.ok("decision", "lock", self.did, "--gate", g3["id"])
        code, p = self.run_cli("decision", "set", self.did, "--metric", "churn_m1")
        self.assertEqual(code, 2)
        self.assertEqual(p["errors"][0]["rule"], "E19")
        self.assertEqual(core.get("option", self.change)["status"], "locked")

    def _lock_with_adr(self, head):
        adr = core.project_dir() / "report" / "70_approval.md"
        adr.parent.mkdir(parents=True, exist_ok=True)
        adr.write_text(f"# {self.did}: решение\n\n{head}\n## Контекст\n\n- текст\n", encoding="utf-8")
        gid = self.answered_gate("Семейная подписка")
        self.ok("decision", "choose", self.did, "--option", self.change, "--gate", gid, "--revise-if", REVISE)
        self.reject_others(self.did)
        g3 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?",
                               "answer": "Утвердить", "answered_by": "human"})
        self.ok("decision", "lock", self.did, "--gate", g3["id"])
        return adr.read_text(encoding="utf-8"), g3["id"]

    def test_lock_keeps_fields_of_one_line_header(self):
        text, g3 = self._lock_with_adr("Статус: предложено (ждёт ворот 3) | Тип двери: двусторонняя | "
                                       "Владелец: директор по продукту\n")
        line = next(x for x in text.splitlines() if x.startswith("Статус:"))
        self.assertRegex(line, rf"^Статус: утверждено человеком \({g3}, \d{{4}}-\d\d-\d\d\) \| "
                               r"Тип двери: двусторонняя \| Владелец: директор по продукту$")

    def test_lock_keeps_bold_block_header(self):
        text, g3 = self._lock_with_adr("**Статус:** предложено (ждёт ворот 3)\n**Тип двери:** односторонняя\n"
                                       "**Владелец:** директор по продукту\n")
        lines = text.splitlines()
        st = next(x for x in lines if x.startswith("**Статус:**"))
        self.assertTrue(st.startswith(f"**Статус:** утверждено человеком ({g3}, "), st)
        self.assertNotIn("предложено", st)
        self.assertIn("**Тип двери:** односторонняя", lines)
        self.assertIn("**Владелец:** директор по продукту", lines)
        self.assertEqual(text.count("**") % 2, 0, "жирное везде закрыто")

    def test_restatus_bold_value_and_dot_separator(self):
        from tools.bulochka.decisions import restatus
        out = restatus("**Статус: предложено** · **Тип двери:** двусторонняя · **Владелец:** CPO", "утверждено")
        self.assertEqual(out, "**Статус: утверждено** · **Тип двери:** двусторонняя · **Владелец:** CPO")
        self.assertEqual(restatus("Статус: утверждено человеком (G1)", "другое"), "Статус: утверждено человеком (G1)")

    def test_supersede_moves_premises_of_unlocked_decision(self):
        c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Не пользуются сервисами")["id"]
        c2 = self.ok("claim", "add", "--type", "assumption", "--text", "Не пользуются сервисами (уточнено)")["id"]
        self.ok("decision", "set", self.did, "--premises", c1)
        data = self.ok("claim", "supersede", c1, "--by", c2)
        self.assertEqual(data["premises_moved"], [self.did])
        self.assertEqual([p["claim_id"] for p in core.get("decision", self.did)["premises"]], [c2])

    def test_due_lists_revise_conditions(self):
        gid = self.answered_gate(self.change)
        self.ok("decision", "choose", self.did, "--option", self.change, "--gate", gid, "--revise-if", REVISE)
        self.assertEqual(self.ok("decision", "due", "--today", "2026-12-01")["due"], [])
        due = self.ok("decision", "due", "--today", "2027-02-02")["due"]
        # условие пересмотра + первое чтение метрики (first_read 2027-01-15)
        self.assertEqual([x["indicator"] for x in due], ["цена Яндекс Плюс", "retention_m2"])

    def test_reject_and_revive_option(self):
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Закрытие триала обвалит приток",
                    "--falsifier", "A/B-тест")["id"]
        revive = json.dumps({"indicator": "приток", "threshold": "<-20%", "assumption_id": c})
        self.ok("option", "reject", self.close, "--killed-by", c, "--revive-if", revive)
        self.assertEqual(core.get("option", self.close)["status"], "rejected")
        c2 = self.ok("claim", "add", "--type", "assumption", "--text", "Приток не зависит от триала",
                     "--falsifier", "тест")["id"]
        self.ok("claim", "supersede", c, "--by", c2)
        code, p = self.run_cli("option", "revive", self.close)  # без ворот — отказ
        self.assertEqual((code, p["refusal"]["tag"]), (2, "HUMAN_DECISION"))
        data = self.ok("option", "revive", self.close, "--gate", self.answered_gate(f"Вернуть {self.close}"))
        self.assertEqual(len(data["met_conditions"]), 1)
        self.assertEqual(core.get("option", self.close)["status"], "open")

    def test_revive_answer_with_unrelated_net(self):
        """«Цифр пока нет… но O### верни» — возврат; «Нет, O### не возвращаем» — отказ (прогон 22.11)."""
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Закрытие триала обвалит приток",
                    "--falsifier", "A/B-тест")["id"]
        revive = json.dumps({"indicator": "приток", "threshold": "<-20%", "assumption_id": c})
        self.ok("option", "reject", self.close, "--killed-by", c, "--revive-if", revive)
        c2 = self.ok("claim", "add", "--type", "assumption", "--text", "Приток не зависит от триала",
                     "--falsifier", "тест")["id"]
        self.ok("claim", "supersede", c, "--by", c2)
        code, p = self.run_cli("option", "revive", self.close, "--gate",
                               self.answered_gate(f"Нет, {self.close} не возвращаем"))
        self.assertEqual((code, p["refusal"]["tag"]), (2, "HUMAN_DECISION"))
        self.ok("option", "revive", self.close, "--gate",
                self.answered_gate(f"Остальных цифр пока нет. Но {self.close} верни в рассмотрение"))
        self.assertEqual(core.get("option", self.close)["status"], "open")

    def test_change_option_requires_distinct_by(self):
        code, _ = self.run_cli("option", "add", "--decision", self.did, "--title", "Что-то новое", "--kind", "change")
        self.assertEqual(code, 3)

    def test_supersede_decision_and_diff(self):
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Новые данные", "--falsifier", "x")["id"]
        d2 = self.ok("decision", "add", "--question", "Что делать с удержанием M2?", "--owner", "продакт подписки",
                     "--metric", "retention_m3", "--first-read", "2027-02-15", "--supersedes", self.did,
                     "--because", c)["id"]
        self.assertEqual(core.get("decision", self.did)["status"], "superseded")
        self.assertEqual(core.get("decision", d2)["vs_previous"], "changed")
        data = self.ok("decision", "diff", d2)
        self.assertIn("metric", data["changed_fields"])
        self.assertTrue((self.tmp / "projects" / "demo" / "report" / "decision-diff.md").exists())


if __name__ == "__main__":
    unittest.main()
