"""Фикстуры фич экономики: проект в шаге research с транскриптом на заданное число токенов и утверждениями."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from cost_fixtures import PRODUCT_ROOT, SID, EconCase, assistant_line

from tools.bulochka import core, cost


class XCase(EconCase):
    STEP_TS = "2026-11-09T09:00:00Z"

    def setUp(self):
        super().setUp()
        os.environ.pop("BULOCHKA_EXT_DIR", None)
        self._actor = os.environ.get("BULOCHKA_ACTOR")
        os.environ["BULOCHKA_ACTOR"] = "agent"

    def tearDown(self):
        if self._actor is None:
            os.environ.pop("BULOCHKA_ACTOR", None)
        else:
            os.environ["BULOCHKA_ACTOR"] = self._actor
        super().tearDown()

    def big_transcript(self, step_tokens=330000):
        """Транскрипт: одно сообщение после начала шага research на step_tokens заголовочных токенов."""
        lines = [assistant_line("big_1", "2026-11-09T09:30:00Z", 0, 30000, 0, step_tokens - 30000)]
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        sub = self.home / "projects" / "-tmp-x" / SID / "subagents" / "agent-a1.jsonl"
        if sub.exists():
            sub.unlink()

    def research_project(self, step_tokens=330000, unverified=("C009", "C012", "C013")):
        self.journal("research", self.STEP_TS, id="J000001", key="k-research")
        for i in range(1, 14):
            cid = f"C{i:03d}"
            rec = {"id": cid, "text": f"утверждение {i}", "type": "fact", "status": "active", "material": True,
                   "verify_minutes": 2}
            if cid not in unverified:
                rec["number_on_page"] = "pass"
            core.put("claim", rec, self.slug)
        self.big_transcript(step_tokens)

    def post_tool_payload(self):
        return {"session_id": SID, "transcript_path": self.transcript, "hook_event_name": "PostToolUse",
                "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(self.root)}

    def cli_json(self, *args):
        r = self.run_cli(*args, "--json")
        try:
            return r, json.loads(r.stdout)
        except ValueError:
            self.fail(f"не JSON: {r.stdout}\n{r.stderr}")

    def run_ext(self, name, payload):
        return self.run_hook(f"ext/{name}", payload)

    def refusals(self):
        return [x for x in core.latest("refusal", self.slug).values() if x.get("tag") == "BUDGET"]

    def cost(self):
        return cost.load_cost(self.slug)
