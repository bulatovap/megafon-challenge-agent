"""Базовый слой: изоляция расширений в тестах, CSV в числах, перенос ссылок при supersede, impact_of по возражениям,
ask до 8 вариантов, BUDGET и выбор продакта, разделы квитанции и одностраничника, слот statusline, PRE_ANSWER,
guard_write по содержимому ADR, пересмотр решения (revision_proposed), core.parse_threshold, профиль check,
metrics.py и number_check.json."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _isolate import wants_isolation
from _ledger_env import PRODUCT_ROOT, LedgerCase, cli, core
from cost_fixtures import EconCase

from tools.bulochka import claims, cost, decisions, export, ext, gates, numbers, project, verify

HOOKS = PRODUCT_ROOT / ".claude" / "hooks"


class ExtDirCase(LedgerCase):
    """LedgerCase + своя папка модулей x_*.py."""

    def setUp(self):
        super().setUp()
        self.ext_dir = self.tmp / "ext_modules"
        self.ext_dir.mkdir()
        self._saved_ext = os.environ.get("BULOCHKA_EXT_DIR")
        os.environ["BULOCHKA_EXT_DIR"] = str(self.ext_dir)
        ext.reset()

    def tearDown(self):
        ext.reset()
        os.environ["BULOCHKA_EXT_DIR"] = self._saved_ext
        super().tearDown()

    def module(self, name, code):
        (self.ext_dir / name).write_text(textwrap.dedent(code), encoding="utf-8")

    def run_cli_err(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json"] + [str(a) for a in argv])
        lines = out.getvalue().strip().splitlines()
        return code, (json.loads(lines[-1]) if lines else {}), err.getvalue()

    def hook(self, name, payload):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        return subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, env=env, timeout=60)


# ---------- 1. изоляция расширений ----------

class TestIsolation(ExtDirCase):
    def test_core_env_points_to_empty_dirs(self):
        # окружение базового слоя без ExtDirCase: пустые папки (проверяем по сохранённому значению)
        self.assertTrue(self._saved_ext and Path(self._saved_ext).is_dir())
        self.assertEqual(list(Path(self._saved_ext).glob("x_*.py")), [])
        hooks_dir = Path(os.environ["BULOCHKA_EXT_HOOKS_DIR"])
        self.assertEqual(list(hooks_dir.glob("*.py")), [])

    def test_reset_purges_registries_of_unloaded_modules(self):
        self.module("x_reg.py", """
            from . import decisions, gates, project, export
            def v(d, o):
                return None
            decisions.PRE_CHOOSE.append(v)
            gates.PRE_ANSWER.append(lambda g, t: None)
            project.RECEIPT_SECTIONS.append(lambda slug: "## Раздел")
            export.ONE_PAGER_SECTIONS.append(lambda slug: "## Раздел")
        """)
        ext.load()
        mods = [f.__module__ for f in decisions.PRE_CHOOSE]
        self.assertIn("tools.bulochka.x_reg", mods)
        ext.reset()
        for reg in (decisions.PRE_CHOOSE, gates.PRE_ANSWER, project.RECEIPT_SECTIONS, export.ONE_PAGER_SECTIONS):
            self.assertFalse([f for f in reg if f.__module__ == "tools.bulochka.x_reg"])

    def test_active_skips_modules_of_other_dir(self):
        self.module("x_one.py", "from . import decisions\ndecisions.PRE_CHOOSE.append(lambda d, o: {'tag': 'X'})\n")
        ext.load()
        self.assertEqual(len([f for f in ext.active(decisions.PRE_CHOOSE)
                              if f.__module__ == "tools.bulochka.x_one"]), 1)
        stale = [f for f in decisions.PRE_CHOOSE if f.__module__ == "tools.bulochka.x_one"]
        other = self.tmp / "other"
        other.mkdir()
        os.environ["BULOCHKA_EXT_DIR"] = str(other)  # смена папки = reset и загрузка заново
        self.assertEqual(ext.load(), [])
        decisions.PRE_CHOOSE.extend(stale)  # как если бы тест расширения импортировал модуль напрямую
        try:
            self.assertFalse([f for f in ext.active(decisions.PRE_CHOOSE) if f in stale])
        finally:
            decisions.PRE_CHOOSE[:] = [f for f in decisions.PRE_CHOOSE if f not in stale]

    def test_extension_tests_not_isolated(self):
        class Fake:
            pass
        f = Fake()
        Fake.__module__ = "test_x_debt"
        self.assertFalse(wants_isolation(f))
        Fake.__module__ = "test_ledger_check"
        self.assertTrue(wants_isolation(f))
        Fake.EXT_ISOLATED = True
        Fake.__module__ = "test_x_debt"
        self.assertTrue(wants_isolation(f))


# ---------- 2. CSV в числах ----------

class TestCsvSafe(unittest.TestCase):
    def test_decimal_with_unit_is_not_csv(self):
        line = "Абоненты:171,8 млн,физлица 92,7%,трафик 93,6%,доходы 2,5 млрд ₽"
        self.assertEqual(numbers._csv_safe(line), line)
        vals = [n["value"] for n in numbers.extract(line)]
        self.assertIn(171800000, vals)

    def test_real_csv_still_csv(self):
        line = "2026-03,промо,12400,2121,0.17"
        self.assertEqual(numbers._csv_safe(line), line.replace(",", ";"))
        self.assertIn(12400, [n["value"] for n in numbers.extract(line)])

    def test_window_cut_uses_full_line(self):
        """Регрессия: обрезок окна без «, » выглядел как CSV, и «171,8» читалось как 171 и 8."""
        head = "Вступление, с запятой и пробелом. " + "Текст без чисел. " * 40
        tail = "показатель 171,8,93,6,288,4 итого"
        page = head + tail
        claim_nums = numbers.extract("Показатель 171,8")
        off = page.index("171,8")
        self.assertEqual(numbers.number_on_page(claim_nums, page, [off, off + 5], window=30), "pass")
        # обрезок сам по себе — как CSV (≥3 запятых, нет «, »): старая логика давала fail
        lo, hi = max(0, off - 30), off + 5 + 30
        self.assertNotEqual(numbers._csv_safe(page[lo:hi]), page[lo:hi])


# ---------- 3. supersede переносит ссылки вариантов ----------

class TestSupersede(ExtDirCase):
    def setUp(self):
        super().setUp()
        self.did = self.add_decision()
        self.o1 = self.add_option(self.did, "Оставить как есть", "keep")
        self.c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Отток 30%", "--link", self.o1,
                          "--falsifier", "x")["id"]
        self.ok("option", "set", self.o1, "--scores",
                json.dumps({"удержание": {"score": 3, "tag": "Гипотеза", "claim_ids": [self.c1]}}))
        self.c2 = self.ok("claim", "add", "--type", "assumption", "--text", "Отток 25%", "--falsifier", "y")["id"]

    def e04(self):
        return [e for e in verify.run_check()["errors"] if e["rule"] == "E04"]

    def test_moves_decision_link_and_scores(self):
        os.environ["BULOCHKA_ACTOR"] = "agent"
        code, p = self.run_cli("claim", "supersede", self.c1, "--by", self.c2)
        self.assertEqual(code, 0, p)
        self.assertEqual(p["data"]["options_moved"], [self.o1])
        self.assertEqual(p["data"]["links_moved"], [self.o1])
        o = core.get("option", self.o1)
        self.assertEqual(o["criteria_scores"]["удержание"]["claim_ids"], [self.c2])
        self.assertIn(self.o1, core.get("claim", self.c2)["decision_link"])
        self.assertEqual(core.get("claim", self.c1)["decision_link"], [])
        self.assertEqual(self.e04(), [])

    def test_locked_option_keeps_refs(self):
        g = self.answered_gate(f"Берём {self.o1}")
        self.ok("decision", "choose", self.did, "--option", self.o1, "--gate", g,
                "--revise-if", json.dumps([{"indicator": "отток", "threshold": "<35%"}]))
        g3 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?", "answer": "Утвердить",
                               "answered_by": "human"})["id"]
        self.reject_others(self.did)
        self.ok("decision", "lock", self.did, "--gate", g3)
        p = self.ok("claim", "supersede", self.c1, "--by", self.c2)
        self.assertEqual(p["options_moved"], [])
        self.assertEqual(core.get("option", self.o1)["criteria_scores"]["удержание"]["claim_ids"], [self.c1])
        self.assertTrue(self.e04())  # для locked E04 — повод к пересмотру

    def test_actor_human_only_for_human(self):
        os.environ["BULOCHKA_ACTOR"] = "agent"
        code, p = self.run_cli("claim", "supersede", self.c1, "--by", self.c2, "--actor", "human")
        self.assertEqual(code, 0)
        self.assertEqual(p["data"]["actor"], "agent")
        self.assertIn("W-ACTOR", [e["rule"] for e in p["errors"]])
        c3 = self.ok("claim", "add", "--type", "assumption", "--text", "Отток 20%", "--falsifier", "z")["id"]
        os.environ["BULOCHKA_ACTOR"] = "human"
        p = self.ok("claim", "supersede", self.c2, "--by", c3, "--actor", "human")
        self.assertEqual(p["actor"], "human")
        self.assertEqual(core.get("claim", self.c2)["superseded_actor"], "human")


# ---------- 4. impact_of ----------

class TestImpact(ExtDirCase):
    def test_objection_to_killed_option_and_target(self):
        did = self.add_decision()
        o1 = self.add_option(did, "Оставить как есть", "keep")
        o2 = self.add_option(did, "Закрыть", "close")
        c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Аудитория уходит", "--falsifier", "x")["id"]
        r1 = self.ok("objection", "add", "--target", o2, "--text", "Закрытие бьёт по выручке", "--severity", "high",
                     "--evidence", c1)["id"]
        self.ok("option", "reject", o1, "--killed-by", r1, "--revive-if", json.dumps([{"assumption_id": c1}]))
        imp = claims.impact_of(c1)
        self.assertIn(r1, imp)
        self.assertIn(o1, imp)   # возражение → вариант, который оно отклонило
        self.assertIn(o2, imp)   # возражение → его цель
        self.assertIn(did, imp)
        self.assertEqual(sorted(claims.impact_of(r1)), sorted([o1, o2, did]))

    def test_superseded_decision_not_affected(self):
        c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Цены растут", "--falsifier", "x")["id"]
        d1 = self.add_decision(premises=c1)
        self.assertIn(d1, claims.impact_of(c1))
        self.add_decision(question="Новая постановка", supersedes=d1, premises=c1)
        self.assertEqual(core.get("decision", d1)["status"], "superseded")
        self.assertNotIn(d1, claims.impact_of(c1))


# ---------- 5. ask: до 8 вариантов ----------

class TestAskOptions(ExtDirCase):
    def test_up_to_eight(self):
        (self.tmp / "projects" / "demo" / "order.md").write_text("# Бриф\n", encoding="utf-8")
        g, created, _ = gates.ask("question", "Что проверить?", [f"C00{i}" for i in range(1, 9)], why="бюджет",
                                  step="research")
        self.assertTrue(created)
        self.assertEqual(len(g["options"]), 8)
        with self.assertRaises(core.BulochkaError):
            gates.ask("question", "Что?", [str(i) for i in range(9)], why="x", step="research")


# ---------- 6. BUDGET и выбор продакта ----------

class TestBudgetGrant(EconCase):
    def payload(self):
        return {"session_id": "s", "transcript_path": self.transcript, "hook_event_name": "UserPromptSubmit",
                "prompt": "продолжай", "cwd": str(self.root)}

    def over_budget(self, budget=1500):
        self.journal("research", "2026-11-09T09:00:00Z")
        cost.compute(self.slug, transcripts=[self.transcript])
        d = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        d["step_budgets"] = {"research": budget}
        core.atomic_write_json(cost.cost_path(self.slug), d)

    def grant(self, budget, closed=False):
        core.atomic_write_json(core.state_dir(self.slug) / "deferred.json", {
            "deferred": ["C002"], "grants": [{"step": "research", "claim": "C001", "budget": budget, "gate_id": "G001",
                                              "refusal_id": "X001", "ts": core.now_iso(), "closed": closed}]})

    def test_active_grant_skips_budget(self):
        self.over_budget(1500)
        self.grant(1500)
        r = self.run_hook("budget.py", self.payload())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(core.latest("refusal", self.slug), {})

    def test_closed_or_stale_grant_does_not_skip(self):
        self.over_budget(1500)
        self.grant(1500, closed=True)
        self.assertIn("BUDGET", self.run_hook("budget.py", self.payload()).stdout)
        self.grant(1000)  # бюджет изменён после выбора — выбор не действует
        self.assertIn("BUDGET", self.run_hook("budget.py", self.payload()).stdout)


# ---------- 7. разделы квитанции и одностраничника ----------

class TestSections(ExtDirCase):
    def setUp(self):
        super().setUp()
        self.module("x_sections.py", """
            from . import export, project

            def receipt(slug):
                return f"## Наш раздел квитанции\\n- проект {slug}"

            def broken(slug):
                raise RuntimeError("сломано")

            def one_pager(slug):
                return "## Бейдж\\nнайдено 9 из 10"

            project.RECEIPT_SECTIONS.extend([broken, receipt])
            export.ONE_PAGER_SECTIONS.append(one_pager)
        """)

    def test_stop_receipt_calls_sections(self):
        r = self.hook("stop_receipt.py", {"hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.tmp / "projects" / "demo" / "STOP_RECEIPT.md").read_text(encoding="utf-8")
        self.assertIn("## Наш раздел квитанции\n- проект demo", text)
        self.assertLess(text.index("Наш раздел"), text.index("**Далее:**"))

    def test_one_pager_sections(self):
        code, p, err = self.run_cli_err("export", "--one-pager")
        self.assertEqual(code, 0, p)
        text = (self.tmp / "projects" / "demo" / "report" / "one_pager.md").read_text(encoding="utf-8")
        self.assertIn("## Бейдж\nнайдено 9 из 10", text)
        self.assertLess(text.index("## Бейдж"), text.index("## Чего стоил прогон"))


# ---------- 8. statusline: слот расширений после «% недели» ----------

class TestStatuslineSlot(EconCase):
    def setUp(self):
        super().setUp()
        self.ext = self.tmp / "ext"
        self.ext.mkdir()
        os.environ["BULOCHKA_EXT_HOOKS_DIR"] = str(self.ext)

    def line(self):
        r = self.run_hook("statusline.py", self.statusline_input(pct=45, ctx=38))
        self.assertEqual(r.returncode, 0, r.stderr)
        return re.sub(r"\033\[[0-9;]*m", "", r.stdout.strip())

    def test_segment_after_week(self):
        (self.ext / "statusline_a.py").write_text("print('≈37 ₽')\n", encoding="utf-8")
        line = self.line()
        self.assertIn("45% нед. | ≈37 ₽ | утв", line)

    def test_ext_segments_cut_first(self):
        (self.ext / "statusline_a.py").write_text("print('A' * 30)\n", encoding="utf-8")
        (self.ext / "statusline_b.py").write_text("print('B' * 30)\n", encoding="utf-8")
        (self.ext / "statusline_c.py").write_text("print('C' * 30)\n", encoding="utf-8")
        line = self.line()
        self.assertLessEqual(len(line), 100)
        self.assertIn("нед.", line)
        self.assertIn("A" * 30, line)  # сегмент расширения важнее хвоста (утв, долг, ctx): хвост уходит первым
        self.assertNotIn("ctx 38%", line)
        self.assertNotIn("C" * 30, line)  # на третий сегмент места нет


# ---------- 9. PRE_ANSWER / POST_ANSWER ----------

class TestPreAnswer(ExtDirCase):
    def setUp(self):
        super().setUp()
        self.module("x_answer.py", """
            from . import core, gates

            def snapshot(g, text):
                core.append_jsonl(core.state_dir() / "snap.jsonl", {"gate": g["id"], "answered": bool(g.get("answer"))})
                return None

            def no_short(g, text):
                if text == "нет":
                    return {"tag": "HUMAN_DECISION", "target_id": g["id"], "reason": "too_short",
                            "phrase": "Ответ слишком короткий", "next_action": "переспросить"}
                return None

            def after(g):
                core.append_jsonl(core.state_dir() / "after.jsonl", {"gate": g["id"], "answer": g["answer"]})

            gates.PRE_ANSWER.extend([snapshot, no_short])
            gates.POST_ANSWER.append(after)
        """)
        self.gid = core.put("gate", {"step": "options", "kind": "question", "question": "?", "options": ["да", "нет"],
                                     "answer": None, "shown_at": core.now_iso()})["id"]

    def test_refusal_blocks_answer(self):
        code, p, _ = self.run_cli_err("answer", self.gid, "--answer", "нет")
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["reason"], "too_short")
        self.assertIsNone(core.get("gate", self.gid)["answer"])
        snaps = core.read_jsonl(core.state_dir() / "snap.jsonl")
        self.assertEqual(snaps, [{"gate": self.gid, "answered": False}])

    def test_handlers_called_before_and_after(self):
        code, p, _ = self.run_cli_err("answer", self.gid, "--answer", "да")
        self.assertEqual(code, 0, p)
        self.assertEqual(core.read_jsonl(core.state_dir() / "snap.jsonl"), [{"gate": self.gid, "answered": False}])
        self.assertEqual(core.read_jsonl(core.state_dir() / "after.jsonl"), [{"gate": self.gid, "answer": "да"}])
        self.run_cli_err("answer", self.gid, "--answer", "да")  # повтор того же ответа — без вызовов
        self.assertEqual(len(core.read_jsonl(core.state_dir() / "snap.jsonl")), 1)


# ---------- 10. guard_write по содержимому ADR ----------

class TestGuardWriteAdr(ExtDirCase):
    def w(self, rel, tool="Edit"):
        return self.hook("guard_write.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s1",
                                            "cwd": str(self.tmp), "tool_input": {"file_path": rel, "content": "x"}})

    def test_adr_mentioning_locked_decision_blocked(self):
        core.put("decision", {"id": "D001", "status": "locked", "question": "?"})
        core.put("decision", {"id": "D002", "status": "chosen", "question": "?"})
        out = self.tmp / "projects" / "demo" / "report"
        (out / "70_approval.md").write_text("# Решение D001\nВыбран O001.\n", encoding="utf-8")
        (out / "71_approval-next.md").write_text("# Решение D002\n", encoding="utf-8")
        (out / "40_playbook.md").write_text("Опирается на D001.\n", encoding="utf-8")
        r = self.w("projects/demo/report/70_approval.md")
        self.assertEqual(r.returncode, 2)
        self.assertIn("D001", r.stderr)
        self.assertEqual(self.w("projects/demo/report/71_approval-next.md").returncode, 0)
        self.assertEqual(self.w("projects/demo/report/40_playbook.md").returncode, 0)  # не ADR
        self.assertEqual(self.w("projects/demo/report/72_approval-new.md", tool="Write").returncode, 0)  # нового нет


# ---------- 11. пересмотр решения ----------

class TestRevisionProposed(ExtDirCase):
    def setUp(self):
        super().setUp()
        self.c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Удержание 0,31", "--falsifier", "x")["id"]
        self.d1 = self.add_decision(premises=self.c1)
        self.opts = [self.add_option(self.d1, "Оставить как есть", "keep"),
                     self.add_option(self.d1, "Закрыть", "close"),
                     self.add_option(self.d1, "Семейный тариф", "change", distinct_by="audience")]
        g = self.answered_gate(f"Берём {self.opts[0]}")
        self.ok("decision", "choose", self.d1, "--option", self.opts[0], "--gate", g,
                "--revise-if", json.dumps([{"indicator": "удержание", "threshold": ">0,30", "assumption_id": self.c1}]))
        g3 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?", "answer": "Утвердить",
                               "answered_by": "human"})["id"]
        self.reject_others(self.d1)
        self.ok("decision", "lock", self.d1, "--gate", g3)
        self.c2 = self.ok("claim", "add", "--type", "assumption", "--text", "Удержание 0,29", "--falsifier", "y")["id"]
        self.ok("claim", "supersede", self.c1, "--by", self.c2)

    def rules(self):
        rep = verify.run_check()
        return {e["rule"] for e in rep["errors"]}, {w["rule"] for w in rep["warnings"]}

    def test_without_proposal_e04(self):
        errs, _ = self.rules()
        self.assertIn("E04", errs)

    def _derived_on_old(self):
        return core.put("claim", {"type": "promise", "text": "Обещание на старой цифре", "status": "active",
                                  "derived_from": [self.c1], "decision_link": [self.opts[0]], "grade": "C"})["id"]

    def test_derived_on_superseded_without_proposal_e04(self):
        cid = self._derived_on_old()
        ids = [e["ids"] for e in verify.run_check()["errors"] if e["rule"] == "E04"]
        self.assertIn([cid, self.c1], ids)

    def test_derived_on_superseded_under_revision_w13(self):
        cid = self._derived_on_old()
        self.ok("decision", "add", "--question", "Пересмотр", "--owner", "продакт", "--metric", "retention",
                "--first-read", "2027-03-01", "--supersedes", self.d1, "--proposed", "--premises", self.c2,
                "--because", self.c2)
        rep = verify.run_check()
        self.assertNotIn("E04", {e["rule"] for e in rep["errors"]})
        self.assertIn([cid, self.c1], [w["ids"][:2] for w in rep["warnings"] if w["rule"] == "W13"])

    def test_proposed_successor(self):
        p = self.ok("decision", "add", "--question", "Пересмотр удержания", "--owner", "продакт", "--metric", "retention",
                    "--first-read", "2027-03-01", "--supersedes", self.d1, "--proposed", "--premises", self.c2,
                    "--because", self.c2)
        d2 = p["id"]
        self.assertEqual(p["status"], "revision_proposed")
        self.assertEqual(core.get("decision", self.d1)["status"], "locked")  # старое в силе
        errs, warns = self.rules()
        self.assertNotIn("E04", errs)
        self.assertIn("W13", warns)
        self.assertFalse(errs & {"E05", "E06", "E07"})  # у черновика-преемника вариантов пока нет
        o = self.add_option(d2, "Оставить как есть", "keep")
        g = core.put("gate", {"step": "options", "kind": "choose_option", "question": "?",
                              "answer": f"Берём {o}", "answered_by": "human"})["id"]
        p = self.ok("decision", "choose", d2, "--option", o, "--gate", g)
        self.assertEqual(p["replaced"], self.d1)
        self.assertEqual(core.get("decision", self.d1)["status"], "superseded")
        errs, _ = self.rules()
        self.assertIn("E05", errs)  # после выбора — обычные правила
        self.assertNotIn("E19", errs)

    def test_proposed_needs_supersedes(self):
        code, p = self.run_cli("decision", "add", "--question", "?", "--owner", "x", "--metric", "m",
                               "--first-read", "2027-01-01", "--proposed")
        self.assertEqual(code, core.EXIT_USAGE)


# ---------- 12. пороги ----------

class TestThreshold(unittest.TestCase):
    def test_parse(self):
        cases = {">0,30": (">", 0.3), "≥ 35%": ("≥", 0.35), "не ниже 2 п.п.": ("≥", 0.02),
                 "< 1,5 млн ₽": ("<", 1500000.0), "<−3 п.п.": ("<", -0.03), "0.3": ("≥", 0.3),
                 "не более 10%": ("≤", 0.1), "=1": ("=", 1.0), "выше 12 345 руб.": (">", 12345.0)}
        for s, want in cases.items():
            op, v = core.parse_threshold(s)
            self.assertEqual(op, want[0], s)
            self.assertAlmostEqual(v, want[1], places=9, msg=s)
        with self.assertRaises(core.BulochkaError):
            core.parse_threshold("высокий")
        self.assertTrue(core.threshold_holds(">", 0.3, 0.31))
        self.assertFalse(core.threshold_holds(">", 0.3, 0.29))


class TestThresholdWarning(ExtDirCase):
    def test_unparseable_threshold_warns(self):
        did = self.add_decision()
        code, p = self.run_cli("decision", "set", did, "--revise-if",
                               json.dumps([{"indicator": "удержание", "threshold": "заметно упало"}]))
        self.assertEqual(code, 0)
        self.assertIn("W-THRESHOLD", [e["rule"] for e in p["errors"]])
        code, p = self.run_cli("decision", "set", did, "--revise-if",
                               json.dumps([{"indicator": "удержание", "threshold": ">0,30", "check_by": "2026-01-01"}]))
        self.assertNotIn("W-THRESHOLD", [e["rule"] for e in p["errors"]])


# ---------- 13. профиль check ----------

class TestCheckProfile(ExtDirCase):
    def test_default_profile_and_no_letter(self):
        cfg = json.loads((self.tmp / "config" / "gate_profile.json").read_text(encoding="utf-8"))
        cfg["default"] = "strict"
        (self.tmp / "config" / "gate_profile.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(verify.run_check()["profile"], "strict")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["check"])
        first = buf.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("Проверка качества: "), first)
        self.assertNotIn("(strict)", first)
        self.assertNotIn("Gate", first)

    def test_no_default_is_default(self):
        (self.tmp / "config" / "gate_profile.json").write_text(json.dumps({"profiles": {}}), encoding="utf-8")
        self.assertEqual(verify.run_check()["profile"], "default")


# ---------- 14. metrics.py ----------

class TestMetricsNumberCheck(ExtDirCase):
    def test_reads_number_check(self):
        sys.path.insert(0, str(PRODUCT_ROOT / "scripts"))
        try:
            import metrics
        finally:
            sys.path.pop(0)
        self.assertNotIn("numbers_total", metrics.project_metrics("demo"))
        core.atomic_write_json(core.state_dir() / "number_check.json",
                               {"found": 45, "total": 46, "became_hypotheses": 2, "items": []})
        m = metrics.project_metrics("demo")
        self.assertEqual((m["numbers_found_on_page"], m["numbers_total"], m["numbers_became_hypotheses"]), (45, 46, 2))
        self.assertEqual(m["numbers_found_pct"], 97.8)


if __name__ == "__main__":
    unittest.main()


# ---------- 12. отклонённый вариант не двигает решение ----------

class TestImpactRejectedOption(ExtDirCase):
    def test_rejected_option_listed_but_decision_not(self):
        from tools.bulochka import claims as claims_mod
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Рынок растёт", "--falsifier", "x")["id"]
        did = self.add_decision()
        o_keep = self.add_option(did, "Оставить как есть", "keep")
        o_bad = self.add_option(did, "Поднять цену", "change", distinct_by="revenue_model")
        self.ok("option", "reject", o_bad, "--killed-by", c,
                "--revive-if", json.dumps([{"assumption_id": c, "threshold": "<10%"}]))
        imp = claims_mod.impact_of(c)
        self.assertIn(o_bad, imp)
        self.assertNotIn(did, imp)
        self.assertNotIn(o_keep, imp)


# ---------- 13. волна правок по живым прогонам B ----------

class TestReviseWhen(LedgerCase):
    def setUp(self):
        super().setUp()
        self.did = self.add_decision()
        self.keep = self.add_option(self.did, "Оставить как есть", "keep")
        self.close = self.add_option(self.did, "Закрыть", "close")
        self.chg = self.add_option(self.did, "Семейный тариф", "change", distinct_by="audience")

    def choose(self, cond):
        g = self.answered_gate(f"Берём {self.keep}")
        return self.run_cli("decision", "choose", self.did, "--option", self.keep, "--gate", g,
                            "--revise-if", json.dumps([cond]))

    def test_revise_when_becomes_holding_threshold(self):
        code, p = self.choose({"indicator": "отписки из-за цены", "revise_when": "≥ 50%"})
        self.assertEqual(code, 0, p)
        cond = core.get("decision", self.did)["revise_if"][0]
        self.assertEqual(cond["threshold"], "<50%")
        w14 = [w for w in verify.run_check()["warnings"] if w["rule"] == "W14"]
        self.assertEqual(w14[0]["ids"], [self.did, self.close, self.chg])
        self.assertEqual(cond["revise_when"], "≥ 50%")

    def test_contradiction_refused(self):
        code, p = self.choose({"indicator": "x", "revise_when": "≥ 50%", "threshold": "≥ 50%"})
        self.assertNotEqual(code, 0)
        self.assertEqual(core.get("decision", self.did)["status"], "draft")

    def test_readback_and_open_alternatives_warning(self):
        os.environ.pop("BULOCHKA_ACTOR", None)
        buf = io.StringIO()
        g = self.answered_gate(f"Берём {self.keep}")
        with contextlib.redirect_stdout(buf):
            cli.main(["decision", "choose", self.did, "--option", self.keep, "--gate", g,
                      "--revise-if", json.dumps([{"indicator": "удержание", "threshold": ">0,30"}])])
        out = buf.getvalue()
        self.assertIn("держится, пока > 0,30; пересмотр, когда ≤ 0,30", out)
        self.assertIn("W14", out)
        self.assertIn("python3 -m tools.bulochka option reject", out)
        self.assertNotIn("▶ Далее: bulochka", out)


class TestVerbatimAnswer(LedgerCase):
    def setUp(self):
        super().setUp()
        os.environ["BULOCHKA_ACTOR"] = "agent"
        from tools.bulochka import gates
        self.gates = gates
        self.gid = gates.ask("question", "Финансы дадут экономику?", ["да", "нет"])[0]["id"]

    def tearDown(self):
        os.environ.pop("BULOCHKA_ACTOR", None)
        super().tearDown()

    def say(self, text):
        core.append_jsonl(core.state_dir() / self.gates.HUMAN_PROMPTS, {"ts": core.now_iso(), "text": text})

    def test_without_prompt_log_not_checked(self):
        self.ok("answer", self.gid, "--answer", "нет")

    def test_paraphrase_refused_verbatim_passes(self):
        self.say("1 — да. 3 — нет, экономики не дадут, финансы отдают только по запросу через месяц")
        code, p = self.run_cli("answer", self.gid, "--answer",
                               "Нет: экономику (ARPU, маржа) финансы дадут только по запросу через месяц")
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["reason"], "answer_not_verbatim")
        self.ok("answer", self.gid, "--answer", "нет, экономики не дадут, финансы отдают только по запросу через месяц")

    def test_answer_by_option_number(self):
        """Прогон 22.11: человек ответил номером варианта — ответ пишется с первого раза, вариант по номеру."""
        g = self.gates.ask("question", "Какие данные есть?", ["Только бриф", "Бриф и открытые ориентиры",
                                                               "Приложу файлы"])[0]["id"]
        self.say("1. Данных сверх брифа нет, оператор вымышленный.")
        d = self.ok("answer", g, "--answer", "1. Данных сверх брифа нет, оператор вымышленный.")
        self.assertEqual(d["answer"], "Только бриф")
        self.assertEqual(d["answer_raw"], "1. Данных сверх брифа нет, оператор вымышленный.")
        self.assertEqual(d["answer_note"], "Данных сверх брифа нет, оператор вымышленный.")
        rec = core.get("gate", g)
        self.assertEqual((rec["answer"], rec["answered_by"]), ("Только бриф", "human"))
        for said in ("2", "2.", "2 — бриф и ориентиры"):
            g2 = self.gates.ask("question", f"Ещё раз ({said})?", ["да", "нет"])[0]["id"]
            self.say(said)
            self.assertEqual(self.ok("answer", g2, "--answer", said)["answer"], "нет", said)

    def test_number_answer_still_verbatim(self):
        self.say("3 — нет")
        code, p = self.run_cli("answer", self.gid, "--answer", "1")  # человек сказал не «1»
        self.assertEqual((code, p["refusal"]["reason"]), (2, "answer_not_verbatim"))
        code, p = self.run_cli("answer", self.gid, "--answer", "да")  # и не «да»
        self.assertEqual(code, 2)

    def test_numbers_that_are_not_option_numbers(self):
        f = self.gates.option_by_number
        opts = ["да", "нет", "не знаю"]
        for text in ("2 семьи", "12 п.п.", "4", "1.5", "2,5 млн", "сто"):
            self.assertEqual(f(text, opts), (text, ""), text)
        self.assertEqual(f("3) не знаю", opts), ("не знаю", "не знаю"))
        self.assertEqual(f("1", []), ("1", ""))

    def test_human_terminal_not_checked(self):
        self.say("что-то другое")
        os.environ["BULOCHKA_ACTOR"] = "human"
        self.ok("answer", self.gid, "--answer", "нет")


class TestPromptHook(LedgerCase):
    def run_hook(self, payload):
        import subprocess
        env = dict(os.environ, BULOCHKA_ROOT=str(self.tmp), CLAUDE_PROJECT_DIR=str(self.tmp))
        hook = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "prompt_context.py"
        r = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_progress_line_and_prompt_logged(self):
        ctx = self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "поправил", "session_id": "s1"})
        self.assertIn("Шаг ", ctx)
        self.assertIn("/11 · ", ctx)
        from tools.bulochka import gates
        self.assertEqual(gates.human_prompts()[-1]["text"], "поправил")


class TestFirstPromptBeforeProject(LedgerCase):
    """Прогон 22.11: первая реплика создаёт проект в том же вызове — хук срабатывал раньше, реплика терялась."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(str(self.tmp / "projects" / "demo"))
        os.environ.pop("BULOCHKA_PROJECT", None)

    run_hook = TestPromptHook.run_hook

    def test_pending_prompt_moves_into_new_project(self):
        from tools.bulochka import gates
        ctx = self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "Вот бриф, нужна стратегия",
                             "session_id": "s1"})
        self.assertIn("Шаг 1/11", ctx)
        pending = self.tmp / "projects" / gates.PENDING_PROMPTS
        self.assertEqual([r["text"] for r in core.read_jsonl(pending)], ["Вот бриф, нужна стратегия"])
        brief = self.tmp / "brief.md"
        brief.write_text("# Бриф\n", encoding="utf-8")
        self.ok("new", "fresh", "--brief", str(brief))
        os.environ["BULOCHKA_PROJECT"] = "fresh"
        self.assertFalse(pending.exists())
        self.assertEqual([p["text"] for p in gates.human_prompts("fresh")], ["Вот бриф, нужна стратегия"])
        self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "Идём.", "session_id": "s1"})
        self.assertEqual([p["text"] for p in gates.human_prompts("fresh")], ["Вот бриф, нужна стратегия", "Идём."])
        self.assertFalse(pending.exists())

    def test_pending_path_is_gitignored(self):
        ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("projects/.pending_prompts.jsonl", ignore)


class TestFullCmd(unittest.TestCase):
    def test_bare_bulochka_expanded_once(self):
        self.assertEqual(core.full_cmd("`bulochka ddiff D002`"), "`python3 -m tools.bulochka ddiff D002`")
        self.assertEqual(core.full_cmd("python3 -m tools.bulochka ddiff"), "python3 -m tools.bulochka ddiff")
        self.assertEqual(core.full_cmd({"a": ["bulochka status"]}), {"a": ["python3 -m tools.bulochka status"]})


class TestJournalOutputPath(LedgerCase):
    def test_root_relative_output_not_doubled(self):
        from tools.bulochka import journal
        slug = core.current_slug()
        out = core.project_dir() / "report" / "20_baseline.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x", encoding="utf-8")
        for given in (f"projects/{slug}/report/20_baseline.md", "report/20_baseline.md", str(out)):
            p, rel = journal.resolve_output(given)
            self.assertEqual(rel, "report/20_baseline.md", given)
            self.assertTrue(p.exists())


class TestReviveViaRevisionGate(LedgerCase):
    def test_revise_question_answer_naming_option_is_enough(self):
        did = self.add_decision()
        keep = self.add_option(did, "Оставить как есть", "keep")
        close = self.add_option(did, "Закрыть", "close")
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Охват 5 750", "--falsifier", "x")["id"]
        self.ok("option", "reject", close, "--killed-by", c, "--revive-if", json.dumps([{"assumption_id": c, "threshold": "<5750"}]))
        g = core.put("gate", {"step": "revise", "kind": "approve_decision", "question": f"Что делаем с {did}?",
                              "options": ["Подтвердить", "Скорректировать", f"Заменить — вернуть {close}"],
                              "answer": f"Вернуть {close} в рассмотрение.", "answered_by": "human"})["id"]
        self.ok("option", "revive", close, "--gate", g)
        self.assertEqual(core.get("option", close)["status"], "open")
        g2 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "Утверждаем?",
                               "options": ["Утвердить"], "answer": "Утвердить", "answered_by": "human"})["id"]
        self.ok("option", "reject", close, "--killed-by", c)
        code, p = self.run_cli("option", "revive", close, "--gate", g2)
        self.assertEqual(code, 2)


class TestStatuslineFit(unittest.TestCase):
    def test_ctx_goes_before_extension_segment(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sl", str(PRODUCT_ROOT / ".claude" / "hooks" / "statusline.py"))
        sl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sl)
        parts = ["этап 6/11", "\033[31mбюджет 85%\033[0m", "1,2 млн ток", "+1% нед.", "утв 84 (48✓)", "долг 0", "возр 3",
                 "отказ 2", "ctx 45%"]
        line = sl.fit_with_ext(parts, ["≈37 ₽ подп."], 4)
        self.assertIn("≈37 ₽ подп.", line)
        self.assertNotIn("ctx", line)
        self.assertLessEqual(sl.vlen(line), sl.MAX_LEN)


class TestResolveByChatGate(LedgerCase):
    def test_uncertain_resolved_by_human_answer_in_chat(self):
        from tools.bulochka import journal
        os.environ["BULOCHKA_ACTOR"] = "agent"
        try:
            _, rec = journal.begin("step.run", {"step": "situation"}, step="situation")
            journal.mark_session_uncertain(core.current_slug(), reason="тест")
            jid = rec["id"]
            code, p = self.run_cli("journal", "resolve", jid, "--as", "committed")
            self.assertEqual(code, 2)
            g = core.put("gate", {"kind": "question", "question": f"{jid}: шаг завершён или повторить?",
                                  "options": ["Завершён", "Повторить"], "answer": "Завершён",
                                  "answered_by": "human"})["id"]
            code, p = self.run_cli("journal", "resolve", jid, "--as", "failed", "--gate", g)
            self.assertEqual(code, 2)  # ответ «завершён» не даёт права записать failed
            self.ok("journal", "resolve", jid, "--as", "committed", "--gate", g)
        finally:
            os.environ.pop("BULOCHKA_ACTOR", None)


class TestDecisionLevelDebt(LedgerCase):
    def test_option_without_own_claims_inherits_decision_debt(self):
        did = self.add_decision()
        o = self.add_option(did, "Оставить как есть", "keep")
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Помощь вовремя в 50% случаев",
                    "--link", did, "--falsifier", "x", "--material")["id"]
        self.assertEqual(decisions.evidence_debt(core.get("option", o)), [c])
        rules = {w["rule"] for w in verify.run_check()["warnings"]}
        self.assertIn("W15", rules)
        self.ok("option", "set", o, "--scores",
                json.dumps({"польза": {"score": 3, "tag": "Факт", "claim_ids": []}}))

    def test_method_source_never_stale(self):
        from tools.bulochka import sources
        self.assertEqual(sources.staleness({"kind": "method", "published": "2010-01-01"})[0], False)
        self.assertEqual(sources.staleness({"kind": "media", "published": "2010-01-01"})[0], True)


class TestSharedIndicatorClaim(LedgerCase):
    def test_one_claim_two_indicators_warns(self):
        from tools.bulochka import decisions as dm
        w = dm.shared_claim_warnings([{"indicator": "SP1", "assumption_id": "C055", "threshold": ">=4%"},
                                      {"indicator": "SP1b", "assumption_id": "C055", "threshold": ">=30%"}], "D001")
        self.assertEqual(w[0]["rule"], "W-INDICATOR")
        self.assertEqual(dm.shared_claim_warnings([{"indicator": "SP1", "assumption_id": "C055"}], "D001"), [])
