"""Точки расширения базового слоя: x_*.py, EXTRA_RULES, quality_gate.local.json, PRE_CHOOSE, ext-хуки,
сегменты statusline, долг доказательств одной функцией, impact_of по revise_if/revive_if, claim --actor,
source.fetch в журнале, guard_write вне корня, бриф без ключа ответов."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _ledger_env import PRODUCT_ROOT, LedgerCase, cli, core
from cost_fixtures import EconCase

from tools.bulochka import claims, cost, decisions, ext, journal, project

HOOKS = PRODUCT_ROOT / ".claude" / "hooks"


class ExtCase(LedgerCase):
    """LedgerCase + своя папка модулей x_*.py (BULOCHKA_EXT_DIR)."""

    def setUp(self):
        super().setUp()
        self._ext_env = os.environ.get("BULOCHKA_EXT_DIR")
        self.ext_dir = self.tmp / "ext_modules"
        self.ext_dir.mkdir()
        os.environ["BULOCHKA_EXT_DIR"] = str(self.ext_dir)
        ext.reset()
        self._pre = list(decisions.PRE_CHOOSE)

    def tearDown(self):
        decisions.PRE_CHOOSE[:] = self._pre
        ext.reset()
        if self._ext_env is None:
            os.environ.pop("BULOCHKA_EXT_DIR", None)
        else:
            os.environ["BULOCHKA_EXT_DIR"] = self._ext_env
        super().tearDown()

    def module(self, name, code):
        (self.ext_dir / name).write_text(textwrap.dedent(code), encoding="utf-8")

    def run_cli_err(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json"] + [str(a) for a in argv])
        lines = out.getvalue().strip().splitlines()
        return code, (json.loads(lines[-1]) if lines else {}), err.getvalue()


class TestCliAndRules(ExtCase):
    def test_x_module_registers_command(self):
        self.module("x_hello.py", """
            from . import core

            def cmd(args):
                return core.emit("hello", {"name": args.name}, as_json=args.json)

            def register(sub):
                p = sub.add_parser("hello")
                p.add_argument("name")
                p.set_defaults(func=cmd)
        """)
        code, p, _ = self.run_cli_err("hello", "мир")
        self.assertEqual(code, 0)
        self.assertEqual(p["data"]["name"], "мир")

    def test_broken_x_module_goes_to_stderr(self):
        self.module("x_broken.py", "raise RuntimeError('сломано')\n")
        self.module("x_bad_register.py", "def register(sub):\n    raise ValueError('нет')\n")
        code, p, err = self.run_cli_err("decision", "list")
        self.assertEqual(code, 0, p)
        self.assertIn("x_broken.py", err)
        self.assertIn("x_bad_register.py", err)

    def test_extra_rules_and_local_config(self):
        self.module("x_rule.py", '''
            def r_x01(ctx):
                """Нет ни одного решения"""
                return [] if ctx.decisions else [{"ids": [], "msg": "решений нет"}]

            def r_x02(ctx):
                raise RuntimeError("падает")

            EXTRA_RULES = {"X01": r_x01, "X02": r_x02, "E01": r_x01}
        ''')
        code, p, err = self.run_cli_err("check")
        self.assertIn("X01", [w["rule"] for w in p["data"]["warnings"]])
        self.assertIn("X02 упало", err)
        self.assertIn("E01", err)  # правило базового слоя расширение не подменяет
        (self.tmp / "config" / "quality_gate.local.json").write_text(json.dumps(
            {"rules": [{"id": "X01", "level": "error", "message": "Нужно решение"}]}), encoding="utf-8")
        code, p, _ = self.run_cli_err("check")
        self.assertEqual(code, 1)
        x01 = [e for e in p["data"]["errors"] if e["rule"] == "X01"]
        self.assertTrue(x01 and x01[0]["msg"].startswith("Нужно решение"))

    def test_local_config_lowers_core_rule(self):
        did = self.add_decision()
        self.add_option(did, "Оставить как есть", "keep")
        _, p, _ = self.run_cli_err("check")
        self.assertIn("E05", [e["rule"] for e in p["data"]["errors"]])
        (self.tmp / "config" / "quality_gate.local.json").write_text(json.dumps(
            {"rules": [{"id": "E05", "level": "off"}]}), encoding="utf-8")
        _, p, _ = self.run_cli_err("check")
        self.assertNotIn("E05", [e["rule"] for e in p["data"]["errors"] + p["data"]["warnings"]])


class TestPreChoose(ExtCase):
    def setUp(self):
        super().setUp()
        self.module("x_guard.py", """
            from . import decisions, refusals

            def no_close(d, o):
                if (o or {}).get("kind") != "close":
                    return None
                cost = refusals.DEFAULT_COST["HUMAN_DECISION"]
                text = refusals.phrase("", "Закрытие не ставлю", "HUMAN_DECISION", "есть долг", "", "снять долг", cost)
                return refusals.record("HUMAN_DECISION", target_id=d["id"], reason="evidence_debt",
                                       phrase_text=text, next_action="bulochka debt " + o["id"], cost=cost)

            decisions.PRE_CHOOSE.append(no_close)
        """)
        self.did = self.add_decision()
        self.keep = self.add_option(self.did, "Оставить как есть", "keep")
        self.close = self.add_option(self.did, "Закрыть", "close")

    def test_validator_refuses_choose(self):
        gid = self.answered_gate(f"Берём {self.close}")
        code, p, _ = self.run_cli_err("decision", "choose", self.did, "--option", self.close, "--gate", gid)
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["reason"], "evidence_debt")
        self.assertEqual(p["next"], f"python3 -m tools.bulochka debt {self.close}")
        self.assertEqual(core.get("decision", self.did)["status"], "draft")
        code, p, _ = self.run_cli_err("option", "choose", self.close, "--gate", gid)
        self.assertEqual(code, 2)

    def test_core_human_decision_first(self):
        code, p, _ = self.run_cli_err("decision", "choose", self.did, "--option", self.close)
        self.assertEqual((code, p["refusal"]["reason"]), (2, "no_gate"))

    def test_other_option_passes_and_lock_checked(self):
        gid = self.answered_gate(f"Берём {self.keep}")
        code, p, _ = self.run_cli_err("decision", "choose", self.did, "--option", self.keep, "--gate", gid)
        self.assertEqual(code, 0, p)
        calls = []
        decisions.PRE_CHOOSE.append(lambda d, o: calls.append((d["id"], o["id"])) or None)
        g3 = core.put("gate", {"step": "decide", "kind": "approve_decision", "question": "?",
                               "answer": "Утвердить", "answered_by": "human"})
        self.reject_others(self.did)
        code, p, _ = self.run_cli_err("decision", "lock", self.did, "--gate", g3["id"])
        self.assertEqual(code, 0, p)
        self.assertEqual(calls, [(self.did, self.keep)])


class TestDebtImpactActor(ExtCase):
    def test_status_debt_counts_option_debt(self):
        did = self.add_decision()
        o1 = self.add_option(did, "Оставить как есть", "keep")
        o2 = self.add_option(did, "Закрыть", "close")
        self.ok("claim", "add", "--type", "assumption", "--text", "Семьи готовы делить", "--link", o1,
                "--falsifier", "интервью")
        self.ok("claim", "add", "--type", "forecast", "--text", "Отток снизится", "--link", f"{o1},{o2}",
                "--p-range", "0.2,0.4", "--horizon", "90d", "--base-rate", "10%")
        self.ok("claim", "add", "--type", "assumption", "--text", "Без связи с вариантом", "--falsifier", "x")
        self.assertEqual(decisions.total_evidence_debt(), 3)
        self.assertEqual(project.compute()["counts"]["evidence_debt"], 3)
        self.ok("option", "reject", o2, "--killed-by", "C002")
        self.assertEqual(project.compute()["counts"]["evidence_debt"], 2)

    def test_impact_follows_revise_and_revive_assumptions(self):
        did = self.add_decision()
        o1 = self.add_option(did, "Оставить как есть", "keep")
        cid = self.ok("claim", "add", "--type", "assumption", "--text", "Цены конкурентов растут",
                      "--falsifier", "x")["id"]
        other = self.ok("claim", "add", "--type", "assumption", "--text", "Иное", "--falsifier", "y")["id"]
        self.ok("decision", "set", did, "--revise-if", json.dumps({"indicator": "цена", "assumption_id": cid}))
        self.ok("option", "reject", o1, "--killed-by", other,
                "--revive-if", json.dumps([{"indicator": "цена", "assumption_id": cid}]))
        self.assertEqual(claims.impact_of(cid), sorted([did, o1]))

    def test_claim_actor_human_only_for_human(self):
        os.environ["BULOCHKA_ACTOR"] = "agent"
        code, p = self.run_cli("claim", "add", "--type", "assumption", "--text", "a", "--falsifier", "x",
                               "--actor", "human")
        self.assertEqual(code, 0)
        self.assertEqual(core.get("claim", p["data"]["id"])["actor"], "agent")
        self.assertIn("W-ACTOR", [e["rule"] for e in p["errors"]])
        os.environ["BULOCHKA_ACTOR"] = "human"
        p = self.ok("claim", "add", "--type", "assumption", "--text", "b", "--falsifier", "x", "--actor", "human")
        self.assertEqual(core.get("claim", p["id"])["actor"], "human")

    def test_source_fetch_logged_in_journal(self):
        code, p = self.run_cli("source", "fetch", "--url", "http://127.0.0.1:9/nothing", "--title", "t",
                               "--publisher", "p", "--timeout", "2")
        ev = [r for r in journal.records() if r.get("operation") == "source.fetch"]
        self.assertEqual(len(ev), 1, p)
        self.assertEqual(ev[0]["url"], "http://127.0.0.1:9/nothing")
        self.assertFalse(ev[0]["ok"])
        self.assertTrue(ev[0]["source_id"].startswith("S"))


class TestGuardWriteRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="guard root-"))
        (self.tmp / "projects").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def w(self, path):
        env = dict(os.environ)
        env.update({"CLAUDE_PROJECT_DIR": str(self.tmp), "BULOCHKA_ROOT": str(self.tmp)})
        env.pop("BULOCHKA_PROJECT", None)
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write", "session_id": "s1", "cwd": str(self.tmp),
                   "tool_input": {"file_path": path, "content": "x"}}
        return subprocess.run([sys.executable, str(HOOKS / "guard_write.py")], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_outside_root_blocked_tmp_allowed(self):
        outside = Path.home() / "bulochka-guard-test-nonexistent" / "analyze.py"
        r = self.w(str(outside))
        self.assertEqual(r.returncode, 2)
        self.assertIn("вне папки проекта", r.stderr)
        for ok in ("/tmp/bulochka-scratch/a.py", "notes/a.md", str(self.tmp / "projects" / "x" / "inbox" / "a.md")):
            self.assertEqual(self.w(ok).returncode, 0, ok)


class TestExtDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ext hooks-"))
        self.ext = self.tmp / "ext"
        self.ext.mkdir()
        (self.tmp / "projects" / "demo" / "state").mkdir(parents=True)
        (self.tmp / "projects" / "CURRENT").write_text("demo\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def script(self, name, body):
        (self.ext / name).write_text(textwrap.dedent(body), encoding="utf-8")

    def run_hook(self, name, payload):
        env = dict(os.environ)
        env.update({"CLAUDE_PROJECT_DIR": str(self.tmp), "BULOCHKA_ROOT": str(self.tmp),
                    "BULOCHKA_EXT_HOOKS_DIR": str(self.ext)})
        env.pop("BULOCHKA_PROJECT", None)
        return subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, env=env, timeout=60)

    def incidents(self):
        return core.read_jsonl(self.tmp / "projects" / "demo" / "state" / "incidents.jsonl")

    def test_contexts_joined_errors_logged(self):
        self.script("a_ctx.py", """
            import json, sys
            d = json.load(sys.stdin)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": d["hook_event_name"],
                                                     "additionalContext": "контекст А"}}))
        """)
        self.script("b_text.py", "import sys; sys.stdin.read(); print('контекст Б')\n")
        self.script("c_crash.py", "raise RuntimeError('упал')\n")
        self.script("_helper.py", "raise SystemExit(2)\n")  # не запускается
        r = self.run_hook("ext_dispatch.py", {"hook_event_name": "UserPromptSubmit", "prompt": "x", "session_id": "s"})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertEqual(out["additionalContext"], "контекст А\n\nконтекст Б")
        inc = self.incidents()
        self.assertTrue(any(i["kind"] == "ext_hook_error" and i.get("ext") == "c_crash.py" for i in inc))

    def test_first_deny_blocks_rest(self):
        marker = self.tmp / "ran.txt"
        self.script("a_deny.py", """
            import json, sys
            sys.stdin.read()
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": "долг"}}))
        """)
        self.script("b_marker.py", f"open({str(marker)!r}, 'w').write('x')\n")
        r = self.run_hook("ext_dispatch.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                              "tool_input": {"command": "ls"}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(marker.exists())

    def test_exit2_blocks(self):
        self.script("a_block.py", "import sys; sys.stdin.read(); sys.stderr.write('стоп'); sys.exit(2)\n")
        r = self.run_hook("ext_dispatch.py", {"hook_event_name": "Stop"})
        self.assertEqual((r.returncode, r.stderr.strip()), (2, "стоп"))

    def test_empty_ext_dir_silent(self):
        r = self.run_hook("ext_dispatch.py", {"hook_event_name": "SessionStart"})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class TestStatuslineExt(EconCase):
    def setUp(self):
        super().setUp()
        self.ext = self.tmp / "ext"
        self.ext.mkdir()
        os.environ["BULOCHKA_EXT_HOOKS_DIR"] = str(self.ext)

    def tearDown(self):
        os.environ.pop("BULOCHKA_EXT_HOOKS_DIR", None)
        super().tearDown()

    def test_segments_appended_and_red_budget(self):
        (self.ext / "statusline_a.py").write_text("print('H2')\n", encoding="utf-8")
        (self.ext / "statusline_b.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        self.journal("research", "2026-11-09T09:00:00Z")
        cost.compute(self.slug, transcripts=[self.transcript])
        d = json.loads(cost.cost_path(self.slug).read_text(encoding="utf-8"))
        d["step_budgets"] = {"research": 1500}
        core.atomic_write_json(cost.cost_path(self.slug), d)
        r = self.run_hook("statusline.py", self.statusline_input(pct=45, ctx=38))
        self.assertEqual(r.returncode, 0, r.stderr)
        line = r.stdout.strip()
        self.assertIn("\033[31mбюджет 84%\033[0m", line)
        self.assertIn("нед. | H2 | утв", line)  # слот расширений — после «% недели»
        import re
        self.assertLessEqual(len(re.sub(r"\033\[[0-9;]*m", "", line)), 100)

    def test_long_segment_trimmed_to_100(self):
        (self.ext / "statusline_long.py").write_text("print('Ж' * 200)\n", encoding="utf-8")
        r = self.run_hook("statusline.py", self.statusline_input(pct=45, ctx=38))
        self.assertLessEqual(len(r.stdout.strip()), 100)


class TestProductFiles(unittest.TestCase):
    def test_claude_md_includes_features_and_short(self):
        lines = (PRODUCT_ROOT / "CLAUDE.md").read_text(encoding="utf-8").rstrip("\n").splitlines()
        self.assertEqual(lines[-1], "@CLAUDE.features.md")
        self.assertLessEqual(len(lines), 150)
        self.assertTrue((PRODUCT_ROOT / "CLAUDE.features.md").is_file())
        self.assertTrue((PRODUCT_ROOT / ".claude" / "hooks" / "ext" / ".gitkeep").is_file())

    def test_dispatcher_registered_after_core_hooks(self):
        s = json.loads((PRODUCT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            cmds = [h["command"] for grp in s[event] for h in grp["hooks"]]
            self.assertTrue(cmds[-1].endswith('ext_dispatch.py"'), event)
            self.assertGreater(len(cmds), 1, event)
        pre = [g["matcher"] for g in s["PreToolUse"] if "ext_dispatch" in g["hooks"][0]["command"]]
        post = [g["matcher"] for g in s["PostToolUse"] if "ext_dispatch" in g["hooks"][0]["command"]]
        self.assertEqual((pre, post), (["Bash|Edit|Write"], ["Bash|Edit|Write|WebFetch"]))

    def test_briefs_without_answer_key(self):
        for n in range(1, 8):
            text = (PRODUCT_ROOT / "knowledge" / "briefs" / f"B0{n}.md").read_text(encoding="utf-8")
            self.assertIn("## Запрос продакта", text)
            self.assertIn("## Какие материалы", text)
            for banned in ("Что ожидается", "Ловушки", "ANSWERS", "_expected"):
                self.assertNotIn(banned, text, f"B0{n}: {banned}")


if __name__ == "__main__":
    unittest.main()
