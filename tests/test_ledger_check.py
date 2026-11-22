"""check (quality gate), verify, refusals."""
from __future__ import annotations

import json
import unittest

from _ledger_env import PAGE, LedgerCase, core

QUOTE = "отток после пробного периода составил 41%"


def rules(payload, level="errors"):
    items = payload["data"][level]
    return {i["rule"] for i in items}


class CheckTest(LedgerCase):
    def full_decision(self):
        did = self.add_decision()
        opts = [self.add_option(did, "Оставить как есть", "keep"),
                self.add_option(did, "Закрыть подписку", "close"),
                self.add_option(did, "Семейная подписка", "change", "audience")]
        for o in opts:
            self.ok("objection", "add", "--target", o, "--text", "Сильнейшее возражение", "--severity", "medium")
        return did, opts

    def test_too_few_options_no_keep_no_close(self):
        did = self.add_decision()
        self.add_option(did, "Семейная подписка", "change", "audience")
        code, p = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertTrue({"E05", "E06", "E07", "E08"} <= rules(p))

    def test_complete_decision_passes_option_rules_and_writes_report(self):
        self.full_decision()
        code, p = self.run_cli("check")
        self.assertFalse({"E05", "E06", "E07", "E08"} & rules(p))
        report = json.loads((self.tmp / "projects" / "demo" / "state" / "quality_gate_report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["score"], max(0, 100 - 10 * len(report["errors"]) - 2 * len(report["warnings"])))
        self.assertEqual(p["data"]["score"], report["score"])
        # у вариантов нет money_line → W06 предупреждение в профиле по умолчанию
        self.assertIn("W06", rules(p, "warnings"))
        self.assertEqual(code, 1 if report["errors"] else 0)

    def test_strict_profile_raises_w06_to_error(self):
        self.full_decision()
        code, p = self.run_cli("check", "--profile", "strict")
        self.assertIn("W06", rules(p))
        self.assertEqual(code, 1)
        self.assertEqual(p["data"]["profile"], "strict")

    def test_strict_profile_evidence_debt_is_error(self):
        did, opts = self.full_decision()
        self.ok("claim", "add", "--type", "assumption", "--text", "Семьи готовы делить подписку", "--link", opts[2],
                "--falsifier", "интервью")
        gid = self.answered_gate(opts[2])
        self.ok("decision", "choose", did, "--option", opts[2], "--gate", gid,
                "--revise-if", '[{"indicator":"доля семей","threshold":"<10%","check_by":"2027-03-01"}]')
        _, core_p = self.run_cli("check")
        self.assertIn("W07", rules(core_p, "warnings"))
        _, a_p = self.run_cli("check", "--profile", "strict")
        self.assertIn("W07", rules(a_p))

    def test_open_objection_blocks_export(self):
        self.full_decision()
        _, p = self.run_cli("check")
        self.assertNotIn("E09", rules(p))
        _, p = self.run_cli("check", "--export")
        self.assertIn("E09", rules(p))

    def test_untagged_number_in_output_text(self):
        (self.tmp / "projects" / "demo" / "report" / "40_playbook.md").write_text(
            "# Стратегия\n\nОтток после триала 41%.\n\n[Факт] Выручка 2,5 млн ₽ [S001].\n"
            "Шаг 3 из 11.\n<!-- no-tag --> Справочно 10%.\n", encoding="utf-8")
        _, p = self.run_cli("check")
        e11 = [e for e in p["data"]["errors"] if e["rule"] == "E11"]
        self.assertEqual([e["ids"] for e in e11], [["40_playbook.md:3"]])

    def test_price_tag_outside_text_rules(self):
        line = "# Документ\n\nОтток после триала 41%.\n"
        report = self.tmp / "projects" / "demo" / "report"
        (report / "90_price_tag.md").write_text(line, encoding="utf-8")
        (report / "20_baseline.md").write_text(line, encoding="utf-8")
        _, p = self.run_cli("check")
        e11 = [e for e in p["data"]["errors"] if e["rule"] == "E11"]
        self.assertEqual([e["ids"] for e in e11], [["20_baseline.md:3"]])

    def test_pilot_design_counts_as_validation_plan(self):
        c = core.put("claim", {"text": "Спрос есть", "type": "assumption", "status": "active", "grade": "C",
                               "source_ids": []})
        core.put("decision", {"question": "Что делать?", "status": "draft", "caveat": "основания слабые",
                              "premises": [{"claim_id": c["id"], "kind": "assumption"}]})
        _, p = self.run_cli("check")
        self.assertIn("E12", rules(p))
        (self.tmp / "projects" / "demo" / "report" / "50_pilot_design.md").write_text("# Пилот\n", encoding="utf-8")
        _, p = self.run_cli("check")
        self.assertNotIn("E12", rules(p))

    def test_promise_table_without_grade(self):
        (self.tmp / "projects" / "demo" / "report" / "40_playbook.md").write_text(
            "## Обещания\n\n| Обещание | Грейд |\n|---|---|\n| Дешевле на 30% [Факт] | [B] |\n| Лучше всех | — |\n",
            encoding="utf-8")
        _, p = self.run_cli("check")
        e10 = [e for e in p["data"]["errors"] if e["rule"] == "E10"]
        self.assertEqual(len(e10), 1)
        self.assertEqual(e10[0]["ids"], ["40_playbook.md:6"])

    def test_fact_rules_and_human_choice_rule(self):
        # fact без offset, записанный в обход CLI, ловится E01; выбор не человеком — E18
        core.put("claim", {"text": "x", "type": "fact", "status": "active", "source_ids": [], "grade": "C"})
        did = self.add_decision()
        d = core.get("decision", did)
        d.update({"status": "chosen", "chosen_by": "agent"})
        core.put("decision", d)
        _, p = self.run_cli("check")
        self.assertTrue({"E01", "E18", "E20"} <= rules(p))


class VerifyAndRefusalsTest(LedgerCase):
    def test_verify_detects_tampered_source(self):
        sid = self.add_source()
        cid = self.ok("claim", "add", "--type", "fact", "--text", "Отток 41%", "--source", sid, "--quote", QUOTE)["id"]
        code, p = self.run_cli("verify", cid)
        self.assertEqual((code, p["data"]["pass"]), (0, [cid]))
        path = self.tmp / "projects" / "demo" / "sources" / f"{sid}.md"
        path.write_text(path.read_text(encoding="utf-8").replace("41%", "14%"), encoding="utf-8")
        code, p = self.run_cli("verify")
        self.assertEqual(code, 1)
        self.assertEqual(p["data"]["fail"], [cid])
        self.assertEqual(core.get("claim", cid)["quote_status"], "fail")

    def test_verify_impact(self):
        sid = self.add_source()
        cid = self.ok("claim", "add", "--type", "fact", "--text", "Отток 41%", "--source", sid, "--quote", QUOTE)["id"]
        data = self.ok("verify", "--impact", sid)
        self.assertEqual(data["impact"], [cid])

    def test_refusals_list_precision_and_resolve(self):
        self.run_cli("claim", "add", "--type", "fact", "--text", "без источника 1")
        self.run_cli("claim", "add", "--type", "fact", "--text", "без источника 2")
        data = self.ok("refusals", "--open")
        self.assertEqual(len(data["items"]), 2)
        self.assertIsNone(data["precision"])
        self.ok("refusals", "--resolve", "X001", "--as", "accepted")
        code, _ = self.run_cli("refusals", "--resolve", "X002", "--as", "overridden")
        self.assertEqual(code, 3)  # override без причины
        self.ok("refusals", "--resolve", "X002", "--as", "overridden", "--reason", "цитата в PDF, сверено вручную")
        data = self.ok("refusals")
        self.assertEqual(data["precision"], 0.5)
        self.assertEqual(self.ok("refusals", "--open")["items"], [])


if __name__ == "__main__":
    unittest.main()
