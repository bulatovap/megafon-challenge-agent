"""Хуки PreToolUse: guard_write, guard_web — через subprocess со stdin JSON."""
from __future__ import annotations

import json
import unittest

from _s_env import StateCase, core, journal, project


class TestGuardWrite(StateCase):
    def setUp(self):
        super().setUp()
        self.new()

    def w(self, path, tool="Write"):
        return self.hook("guard_write.py", {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s1",
                                            "cwd": str(self.tmp), "tool_input": {"file_path": path, "content": "x"}})

    def test_blocks_env_secrets_and_state(self):
        for path in (".env", "config/.env.local", str(self.tmp / "secrets" / "key.txt"),
                     "projects/demo/state/claims.jsonl", str(self.tmp / "projects/demo/state/state.json")):
            r = self.w(path)
            self.assertEqual(r.returncode, 2, path)
            self.assertTrue(r.stderr.strip(), path)
        self.assertIn("bulochka", self.w("projects/demo/state/journal.jsonl").stderr)
        self.assertTrue(any(i["kind"] == "write_blocked" for i in self.incidents()))
        self.assertTrue(any(r["operation"] == "write.guard" for r in journal.records()))

    def test_allows_report_inbox_and_synthetic(self):
        for path in ("projects/demo/report/20_baseline.md", "projects/demo/inbox/note.md",
                     "projects/demo/interviews/synthetic/r1.md", "README.md", "docs/env.md"):
            self.assertEqual(self.w(path).returncode, 0, path)

    def test_blocks_locked_decision_file(self):
        core.put("decision", {"id": "D001", "status": "locked", "question": "?"})
        core.put("decision", {"id": "D002", "status": "chosen", "question": "?"})
        r = self.w("projects/demo/report/D001-decision.md", tool="Edit")
        self.assertEqual(r.returncode, 2)
        self.assertIn("locked", r.stderr)
        self.assertEqual(self.w("projects/demo/report/D002-decision.md").returncode, 0)

    def test_bad_input_is_exit0_with_incident(self):
        r = self.hook("guard_write.py", None, raw="{не json")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(any(i["kind"] == "hook_error" for i in self.incidents()))


class TestGuardWeb(StateCase):
    def setUp(self):
        super().setUp()
        self.new()
        (self.tmp / "config" / "sources.allow").write_text("# домены\ncbr.ru\nrbc.ru  # СМИ\n", encoding="utf-8")

    def fetch(self, url, tool="WebFetch", **extra):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s1",
                   "tool_input": {"url": url, "prompt": "извлеки цифры"} if tool == "WebFetch" else {"query": url}}
        payload.update(extra)
        out = self.hook_json(self.hook("guard_web.py", payload))
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision")

    def test_allowlist_and_grey(self):
        self.assertEqual(self.fetch("https://www.cbr.ru/statistics/"), "allow")
        self.assertEqual(self.fetch("https://news.rbc.ru/x"), "allow")
        self.assertEqual(self.fetch("https://evil-cbr.ru/x"), "ask")
        self.assertIsNone(self.fetch("тарифы мегафон", tool="WebSearch"))
        ev = [r for r in journal.records() if r["operation"] == "web.guard"]
        self.assertEqual([e["decision"] for e in ev], ["ask"])

    def test_secret_denied(self):
        self.assertEqual(self.fetch("https://cbr.ru/x?api_key=xxxxxxxx"), "deny")
        self.assertEqual(self.fetch("sk-abcdefghijklmnopqrstuv", tool="WebSearch"), "deny")

    def test_frozen_denies_also_in_subagent(self):
        project.set_no_web(True)
        self.assertEqual(self.fetch("https://cbr.ru/x", agent_id="a1", agent_type="researcher"), "deny")
        self.assertEqual(self.fetch("что угодно", tool="WebSearch"), "deny")
        ev = [r for r in journal.records() if r["operation"] == "web.guard"]
        self.assertEqual(ev[0]["agent_type"], "researcher")
        project.set_no_web(False)
        self.assertEqual(self.fetch("https://cbr.ru/x"), "allow")


if __name__ == "__main__":
    unittest.main()
