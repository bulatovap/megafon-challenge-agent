"""Тесты cost.py: токены из транскрипта, дедуп, шаги, недельный лимит, деньги как доля подписки."""
from __future__ import annotations

import json
import unittest

from cost_fixtures import SID, EconCase

from tools.bulochka import core, cost


class TestTranscript(EconCase):
    def test_dedup_by_message_id_and_subagents(self):
        msgs = cost.read_messages(cost.transcript_files(self.transcript))
        self.assertEqual(sorted(msgs), ["msg_1", "msg_2", "msg_3", "msg_s1"])
        total = cost.zero()
        for m in msgs.values():
            total = cost.add(total, m["tokens"])
        self.assertEqual(total, {"in": 42, "out": 420, "cache_read": 4200, "cache_write": 800})

    def test_transcript_files_include_subagents(self):
        files = cost.transcript_files(self.transcript)
        self.assertEqual(len(files), 2)
        self.assertTrue(str(files[1]).endswith("subagents/agent-a1.jsonl"))

    def test_headline_excludes_cache_read(self):
        self.assertEqual(cost.headline({"in": 1, "out": 2, "cache_read": 1000, "cache_write": 3}), 6)

    def test_find_transcript_by_session_id(self):
        self.assertEqual(cost.find_transcript(SID), self.transcript)
        self.assertIsNone(cost.find_transcript("нет-такой"))

    def test_parse_ts_variants(self):
        a = cost.parse_ts("2026-11-09T10:00:05.100Z")
        b = cost.parse_ts("2026-11-09T10:00:05Z")
        self.assertAlmostEqual(a - b, 0.1, places=3)
        self.assertIsNone(cost.parse_ts("не дата"))


class TestSteps(EconCase):
    def test_tokens_split_by_journal_steps(self):
        self.journal("intake", "2026-11-09T10:30:00Z")
        self.journal("research", "2026-11-09T11:15:00Z")
        d = cost.compute(self.slug, transcripts=[self.transcript], write=False)
        self.assertEqual(cost.headline(d["tokens_by_step"]["intake"]), 620)
        self.assertEqual(d["tokens_by_step"]["research"], {"in": 12, "out": 120, "cache_read": 1200, "cache_write": 100})
        self.assertEqual(cost.headline(d["tokens_pre_project"]), 410)  # msg_1 до первой записи журнала
        self.assertEqual(d["tokens"], 852)
        self.assertEqual(d["step"], "research")
        self.assertEqual(cost.headline(d["tokens_step"]), 232)

    def test_without_journal_everything_counts(self):
        d = cost.compute(self.slug, transcripts=[self.transcript], write=False)
        self.assertEqual(d["tokens"], 1262)
        self.assertIsNone(d["step"])
        self.assertEqual(d["messages_counted"], 4)
        self.assertIsNone(d["budget_used_pct"])  # без шага бюджет шага не считается


class TestWeeklyLimit(EconCase):
    def test_delta_same_week(self):
        s = [{"ts": "2026-11-09T10:00:00Z", "pct": 45, "resets_at": 1},
             {"ts": "2026-11-09T11:00:00Z", "pct": 46, "resets_at": 1},
             {"ts": "2026-11-09T12:00:00Z", "pct": 48, "resets_at": 1}]
        w = cost.weekly_delta(s)
        self.assertEqual((w["before"], w["after"], w["delta"]), (45, 48, 3))

    def test_delta_across_week_reset(self):
        s = [{"ts": "2026-11-09T10:00:00Z", "pct": 97, "resets_at": 1},
             {"ts": "2026-11-09T11:00:00Z", "pct": 99, "resets_at": 1},
             {"ts": "2026-11-09T12:00:00Z", "pct": 2, "resets_at": 2}]
        self.assertEqual(cost.weekly_delta(s)["delta"], 4)

    def test_money_is_subscription_share(self):
        usd, rub = cost.money_from_pct(1)
        # $200 × 12/52 = $46.15 в неделю; 1% = $0.46 ≈ 37 ₽ по курсу 81
        self.assertEqual(usd, 0.46)
        self.assertEqual(rub, 37)
        usd, rub = cost.money_from_pct(100)
        self.assertEqual(rub, 3738)
        self.assertEqual(cost.money_from_pct(None), (None, None))

    def test_limit_sample_written_only_on_change(self):
        self.assertTrue(cost.record_limit_sample(45, 7, SID, self.slug))
        self.assertFalse(cost.record_limit_sample(45, 7, SID, self.slug))
        self.assertTrue(cost.record_limit_sample(46, 7, SID, self.slug))
        self.assertEqual(len(core.read_jsonl(cost.limits_path(self.slug))), 2)


class TestCostJson(EconCase):
    def test_cost_json_contract_fields(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        cost.compute(self.slug, stdin=self.statusline_input(pct=45))
        d = cost.compute(self.slug, stdin=self.statusline_input(pct=47))
        saved = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        for k in ("ts", "session_id", "model", "step", "tokens_total", "tokens_step", "step_budget_tokens",
                  "budget_used_pct", "weekly_limit_pct_before", "weekly_limit_pct_after", "plan",
                  "plan_price_month", "plan_price_date", "rub_equiv_total", "fx_rate", "fx_date",
                  "statusline_cost_usd", "context_used_pct", "minutes_human", "estimate", "forecast_error_pct"):
            self.assertIn(k, saved, k)
        self.assertEqual(d["weekly_limit_pct_delta"], 2)
        self.assertEqual(d["rub_equiv_total"], 75)
        self.assertEqual(d["sessions"][SID], self.transcript)
        self.assertEqual(saved["statusline_cost_usd"], 1.23)
        self.assertEqual(saved["model"], "claude-opus-5-5")

    def test_minutes_human_from_gates(self):
        core.put("gate", {"step": "plan", "review_seconds": 90}, self.slug)
        core.put("gate", {"step": "options", "review_seconds": 30}, self.slug)
        self.assertEqual(cost.minutes_human(self.slug), 2.0)


class TestCostCli(EconCase):
    def test_cli_json(self):
        r = self.run_cli("--json", "cost", "--transcript", self.transcript)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["tokens"], 1262)
        self.assertIn("rub_equiv", out["data"])

    def test_cli_without_project_is_usage_error(self):
        (self.root / "projects" / "CURRENT").unlink()
        r = self.run_cli("cost")
        self.assertEqual(r.returncode, 3)

    def test_estimate_then_forecast_error(self):
        brief = self.root / "бриф.md"
        brief.write_text("а" * 100, encoding="utf-8")
        r = self.run_cli("--json", "cost", "--estimate", "--brief", str(brief))
        self.assertEqual(r.returncode, 0, r.stderr)
        est = json.loads(r.stdout)["data"]["estimate"]
        self.assertEqual(est["tokens"], 11 * 60000 + 400)
        d = cost.compute(self.slug, transcripts=[self.transcript])
        self.assertEqual(d["estimate"]["tokens"], 660400)
        self.assertAlmostEqual(d["forecast_error_pct"], round((1262 - 660400) / 660400 * 100, 1))

    def test_set_budget_for_step(self):
        self.journal("research", "2026-11-09T09:00:00Z")
        r = self.run_cli("cost", "--step", "--set-budget", "1000", "--transcript", self.transcript)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        self.assertEqual(d["step_budget_tokens"], 1000)
        self.assertIn("Шаг research", r.stdout)


if __name__ == "__main__":
    unittest.main()
