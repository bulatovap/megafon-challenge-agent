"""Бюджетный предохранитель: отказ посреди шага (PostToolUse), ворота продакта, исполнение ответа, квитанция."""
from __future__ import annotations

import json
import unittest

from x_fixtures import PRODUCT_ROOT, XCase

from tools.bulochka import core, cost


class TestPostToolBudget(XCase):
    def test_hook_gives_budget_context_with_three_claims(self):
        self.research_project()
        r = self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        ctx = out["additionalContext"]
        self.assertIn("BUDGET", ctx)
        for cid in ("C009", "C012", "C013"):
            self.assertIn(cid, ctx)
        self.assertIn("82%", ctx)
        self.assertIn("budget ask", ctx)

    def test_two_calls_one_refusal(self):
        self.research_project()
        self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.run_ext("post_tool_budget.py", self.post_tool_payload())
        refs = self.refusals()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["unverified_ids"], ["C009", "C012", "C013"])
        self.assertEqual(refs[0]["spent"], 330000)

    def test_below_threshold_silent(self):
        self.research_project(step_tokens=200000)
        r = self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(self.refusals(), [])

    def test_silent_after_gate_asked(self):
        self.research_project()
        self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.cli_json("budget", "ask")
        r = self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.assertEqual(r.stdout.strip(), "")

    def test_dispatcher_passes_context(self):
        self.research_project()
        r = self.run_hook("ext_dispatch.py", self.post_tool_payload())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BUDGET", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])


class TestBudgetGate(XCase):
    def asked(self):
        self.research_project()
        self.run_ext("post_tool_budget.py", self.post_tool_payload())
        r, d = self.cli_json("budget", "ask")
        self.assertEqual(r.returncode, 0, r.stderr)
        return d["data"]

    def test_ask_options(self):
        d = self.asked()
        self.assertTrue(d["gate_id"].startswith("G"))
        opts = d["options"]
        self.assertEqual(len(opts), 5)
        self.assertEqual([o[:4] for o in opts[:3]], ["C009", "C012", "C013"])
        self.assertTrue(opts[3].startswith("поднять бюджет до"))
        self.assertEqual(opts[4], "остановиться")
        g = core.get("gate", d["gate_id"], self.slug)
        self.assertEqual(g["why_ask"], "бюджет шага на исходе")
        self.assertEqual(g["default_if_no_answer"], "остановиться")
        self.assertEqual(core.get("refusal", g["refusal_id"], self.slug)["gate_id"], d["gate_id"])
        self.assertEqual(g["claims_shown"], [])  # варианты — не проверка продактом, в минуты не идут

    def test_ask_seven_options_through_core_ask(self):
        """5 непроверенных + «поднять» + «остановиться» = 7 вариантов: ворота ставит `ask` базового слоя (до 8)."""
        five = ("C003", "C005", "C009", "C012", "C013")
        self.research_project(unverified=five)
        self.run_ext("post_tool_budget.py", self.post_tool_payload())
        r, d = self.cli_json("budget", "ask")
        self.assertEqual(r.returncode, 0, r.stderr)
        opts = d["data"]["options"]
        self.assertEqual(len(opts), 7)
        self.assertEqual([o[:4] for o in opts[:5]], list(five))
        g = core.get("gate", d["data"]["gate_id"], self.slug)
        self.assertEqual((g["kind"], g["actor"], g["default_if_no_answer"]), ("question", "agent", "остановиться"))
        self.assertTrue(g["flips"])

    def test_ask_is_idempotent(self):
        d = self.asked()
        _, d2 = self.cli_json("budget", "ask")
        self.assertEqual(d2["data"]["gate_id"], d["gate_id"])
        self.assertFalse(d2["data"]["created"])

    def test_ask_without_refusal_is_usage_error(self):
        self.research_project(step_tokens=1000)
        r = self.run_cli("budget", "ask")
        self.assertEqual(r.returncode, 3)

    def test_resolve_claim(self):
        d = self.asked()
        self.run_cli("answer", d["gate_id"], "--answer", "C012")
        r, out = self.cli_json("budget", "resolve", d["gate_id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        res = out["data"]
        self.assertEqual(res["deferred"], ["C009", "C013"])
        self.assertEqual(res["budget_after"] - res["budget_before"], res["claim_tokens"])
        self.assertEqual(self.cost()["step_budgets"]["research"], 400000 + res["claim_tokens"])
        self.assertEqual(self.refusals()[0]["outcome"], "accepted")
        dfr = json.loads((self.root / "projects" / self.slug / "state" / "deferred.json").read_text(encoding="utf-8"))
        self.assertEqual(dfr["deferred"], ["C009", "C013"])
        _, again = self.cli_json("budget", "resolve", d["gate_id"])
        self.assertTrue(again["data"]["repeated"])
        self.assertEqual(len(dfr["items"]), 2)

    def test_resolve_raise(self):
        d = self.asked()
        self.run_cli("answer", d["gate_id"], "--answer", "4")  # номер варианта → «поднять бюджет до …»
        _, out = self.cli_json("budget", "resolve", d["gate_id"])
        res = out["data"]
        self.assertEqual(res["decision"], "raise")
        self.assertGreater(res["budget_after"], 400000)
        ref = self.refusals()[0]
        self.assertEqual(ref["outcome"], "overridden")
        self.assertIn("поднял бюджет", ref["override_reason"])

    def test_resolve_stop_writes_receipt(self):
        d = self.asked()
        self.run_cli("answer", d["gate_id"], "--answer", "остановиться")
        _, out = self.cli_json("budget", "resolve", d["gate_id"])
        self.assertEqual(out["data"]["deferred"], ["C009", "C012", "C013"])
        pdir = self.root / "projects" / self.slug
        text = (pdir / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        for h in ("## Готово", "## Наполовину", "## Уже можно пользоваться", "## Продолжить одной командой"):
            self.assertIn(h, text)
        self.assertIn('claude "продолжай"', text)
        self.assertIn("C012", text)
        self.assertTrue((pdir / "RESUME.md").exists())

    def test_core_budget_hook_no_repeat_after_choice(self):
        """После выбора продакта (grant в state/deferred.json) хук бюджета базового слоя повторный BUDGET не заводит."""
        d = self.asked()
        self.run_cli("answer", d["gate_id"], "--answer", "C012")
        self.run_cli("budget", "resolve", d["gate_id"])
        payload = dict(self.post_tool_payload(), hook_event_name="UserPromptSubmit", prompt="дальше")
        r = self.run_hook("budget.py", payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(len(self.refusals()), 1)

    def test_grant_then_stop_signal_once(self):
        d = self.asked()
        self.run_cli("answer", d["gate_id"], "--answer", "C012")
        self.run_cli("budget", "resolve", d["gate_id"])
        r = self.run_ext("post_tool_budget.py", self.post_tool_payload())
        self.assertEqual(r.stdout.strip(), "")  # C012 ещё не проверено — молчим, нового отказа нет
        self.assertEqual(len(self.refusals()), 1)
        c = dict(core.get("claim", "C012", self.slug))
        c["number_on_page"] = "pass"
        core.put("claim", c, self.slug)
        r = self.run_ext("post_tool_budget.py", self.post_tool_payload())
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("C012 проверено", ctx)
        self.assertIn("receipt", ctx)


class TestCliHelp(XCase):
    def test_help_of_all_feature_commands(self):
        for cmd in (["budget", "ask"], ["budget", "resolve"], ["receipt"], ["smeta"], ["flip"], ["pricetag"]):
            r = self.run_cli(*cmd, "--help")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("не загружен", r.stderr)
            self.assertNotIn("не выполнен", r.stderr)


class TestReceiptAndStatusline(XCase):
    def test_receipt_sections(self):
        self.research_project(step_tokens=1000)
        pdir = self.root / "projects" / self.slug
        (pdir / "report" / "01_intake.md").write_text("# Бриф\nВсё по делу.\n", encoding="utf-8")
        self.journal("intake", "2026-11-09T08:00:00Z", id="J000002", key="k-intake", status="committed",
                     output_path="report/01_intake.md")
        r, out = self.cli_json("receipt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["data"]["usable"], ["report/01_intake.md"])
        text = (pdir / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        self.assertIn("# Квитанция остановки", text)  # базовая часть сохранена
        self.assertIn("шаг research начат и не закрыт", text)
        self.assertEqual(text.count("## Наполовину"), 1)
        self.cli_json("receipt")  # повтор не дублирует разделы
        self.assertEqual((pdir / "STOP_RECEIPT.md").read_text(encoding="utf-8").count("## Готово"), 1)

    def test_receipt_and_resume_carry_continuation_price(self):
        """Прогон 22.11: в квитанции «стоп» не было цены продолжения."""
        self.research_project(step_tokens=100000)
        pdir = self.root / "projects" / self.slug
        brief = pdir / "order.md"
        brief.write_text("# Бриф\nШаги: 1–5\n", encoding="utf-8")
        self.cli_json("smeta", "--brief", str(brief))
        cost.compute(self.slug, transcripts=[self.transcript])
        est = cost.load_cost(self.slug)["estimate"]
        r, out = self.cli_json("receipt")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (pdir / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Цена продолжения"), 1)
        self.assertLess(text.index("## Цена продолжения"), text.index("## Продолжить одной командой"))
        self.assertIn("Потрачено к остановке (spent): 100k ток", text)
        self.assertIn("недельный лимит: замеров нет", text)
        self.assertIn("мин продакта", text)
        from tools.bulochka import x_receipt
        price = x_receipt.continuation_price(self.slug)
        self.assertEqual(price["left_steps"], est["steps"])  # ни один шаг не закрыт
        research_left = max(0, est["tokens_by_step"]["research"] - 100000)
        self.assertEqual(price["left_by_step"]["research"], research_left)  # начатый шаг — только его остаток
        self.assertEqual(price["left_tokens"], sum(est["tokens_by_step"].values()) - est["tokens_by_step"]["research"] + research_left)
        self.assertIn(f"Остаток по смете (left_by_smeta): ~{cost.fmt_tok(price['left_tokens'])} ток", text)
        self.run_cli("pause")
        resume = (pdir / "RESUME.md").read_text(encoding="utf-8")
        self.assertIn("## Цена продолжения", resume)
        self.assertIn("(spent): 100k ток", resume)
        self.assertIn("(left_by_smeta): ~", resume)
        self.assertLess(resume.index("## Цена продолжения"), resume.index("**Следующее действие:**"))

    def test_receipt_price_without_smeta(self):
        self.research_project(step_tokens=1000)
        self.cli_json("receipt")
        text = (self.root / "projects" / self.slug / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        self.assertIn("(left_by_smeta): сметы нет", text)

    def test_stop_hook_appends_sections(self):
        """Разделы встают через project.RECEIPT_SECTIONS хуком Stop базового слоя — без ext-хука и без ожидания."""
        self.research_project(step_tokens=1000)
        self.assertFalse((PRODUCT_ROOT / ".claude" / "hooks" / "ext" / "stop_receipt_ext.py").exists())
        r = self.run_hook("stop_receipt.py", {"session_id": "s", "hook_event_name": "Stop", "cwd": str(self.root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.root / "projects" / self.slug / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        self.assertIn("## Продолжить одной командой", text)
        self.assertLess(text.index("## Продолжить одной командой"), text.index("**Далее:**"))

    def test_statusline_red_budget_and_week(self):
        self.research_project()
        p = self.statusline_input(pct=45)
        p["transcript_path"] = self.transcript
        self.run_hook("statusline.py", p)
        r = self.run_hook("statusline.py", self.statusline_input(pct=46))
        line = r.stdout.strip()
        self.assertIn("\033[31mбюджет 82%\033[0m", line)
        self.assertIn("+1% нед.", line)  # первый экран: доля недели не вытесняется


    def test_statusline_rub_segment_after_week(self):
        """«≈N ₽ подп.» — сегмент расширения в слоте после «% недели», перед ctx; строка ≤100 символов."""
        self.research_project(step_tokens=200000)  # бюджет не на исходе — обычная строка
        p = self.statusline_input(pct=45)
        p["transcript_path"] = self.transcript
        self.run_hook("statusline.py", p)
        r = self.run_hook("statusline.py", self.statusline_input(pct=46))
        line = r.stdout.strip()
        self.assertRegex(line, r"\+1% нед\. \| ≈\d+ ₽ подп\. \| утв")
        self.assertLessEqual(len(line), 100)

if __name__ == "__main__":
    unittest.main()
