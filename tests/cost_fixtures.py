"""Фикстуры для тестов экономики: временный корень, проект, транскрипт Claude Code с субагентом."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from tools.bulochka import core  # noqa: E402
from _isolate import isolate_ext  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def assistant_line(mid, ts, inp, out, cr, cw, text="x", sidechain=False, model="claude-opus-5-5"):
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain, "timestamp": ts, "sessionId": SID,
        "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                              "cache_creation_input_tokens": cw}},
    }, ensure_ascii=False)


class EconCase(unittest.TestCase):
    """Корень с кириллицей и пробелом в пути, проект demo, CLAUDE_CONFIG_DIR с транскриптом."""

    slug = "demo"

    def setUp(self):
        isolate_ext(self)  # расширения x_*.py и ext-хуки копии не влияют на тесты базового слоя
        self.tmp = Path(tempfile.mkdtemp(prefix="econ-"))
        self.root = self.tmp / "папка проекта"
        (self.root / "config").mkdir(parents=True)
        shutil.copy(PRODUCT_ROOT / "config" / "pricing.json", self.root / "config" / "pricing.json")
        (self.root / "projects" / self.slug / "state").mkdir(parents=True)
        (self.root / "projects" / self.slug / "report").mkdir(parents=True)
        (self.root / "projects" / "CURRENT").write_text(self.slug + "\n", encoding="utf-8")
        self.home = self.tmp / "claude-home"
        self._env = {k: os.environ.get(k) for k in ("BULOCHKA_ROOT", "BULOCHKA_PROJECT", "CLAUDE_CONFIG_DIR")}
        os.environ["BULOCHKA_ROOT"] = str(self.root)
        os.environ.pop("BULOCHKA_PROJECT", None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        self.transcript = self.write_transcript()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Транскрипт: msg_1 записан двумя строками (два блока контента), msg_2, msg_3; у субагента msg_s1 и
    # копия msg_s1. Итого уникальных: in 10+20+5+7=42, out 100+200+50+70=420, cr 1000+2000+500+700=4200,
    # cw 300+400+0+100=800.
    def write_transcript(self):
        d = self.home / "projects" / "-tmp-x"
        d.mkdir(parents=True)
        tp = d / f"{SID}.jsonl"
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-11-09T10:00:00Z", "message": {"role": "user",
                                                                                          "content": "usage тест"}}),
            assistant_line("msg_1", "2026-11-09T10:00:05.100Z", 10, 100, 1000, 300, "блок1"),
            assistant_line("msg_1", "2026-11-09T10:00:05.200Z", 10, 100, 1000, 300, "блок2"),
            "{битая строка с \"usage\"",
            assistant_line("msg_2", "2026-11-09T11:00:00.000Z", 20, 200, 2000, 400),
            assistant_line("msg_3", "2026-11-09T12:00:00Z", 5, 50, 500, 0),
            assistant_line("syn", "2026-11-09T12:00:01Z", 999, 999, 0, 0, model="<synthetic>"),
        ]
        tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sub = d / SID / "subagents"
        sub.mkdir(parents=True)
        s = assistant_line("msg_s1", "2026-11-09T11:30:00Z", 7, 70, 700, 100, sidechain=True)
        (sub / "agent-a1.jsonl").write_text(s + "\n" + s + "\n", encoding="utf-8")
        return str(tp)

    def journal(self, step, ts, **kw):
        rec = {"id": kw.pop("id", None) or core.next_id("journal", self.slug), "step": step, "ts": ts,
               "operation": kw.pop("operation", "step.run"), "status": kw.pop("status", "started"),
               "actor": kw.pop("actor", "agent"), "session_id": SID}
        rec.update(kw)
        core.append_jsonl(core.entity_path("journal", self.slug), rec)

    def statusline_input(self, pct=45, resets_at=1794412800, ctx=63):
        return {"session_id": SID, "transcript_path": self.transcript, "cwd": str(self.root),
                "model": {"id": "claude-opus-5-5"}, "cost": {"total_cost_usd": 1.23},
                "context_window": {"used_percentage": ctx},
                "rate_limits": {"seven_day": {"used_percentage": pct, "resets_at": resets_at}}}

    def run_hook(self, name, payload, cwd=None):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(PRODUCT_ROOT)
        return subprocess.run([sys.executable, str(PRODUCT_ROOT / ".claude" / "hooks" / name)],
                              input=json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload,
                              capture_output=True, text=True, env=env, cwd=cwd or str(self.root), timeout=60)

    def run_cli(self, *args):
        env = dict(os.environ)
        return subprocess.run([sys.executable, "-m", "tools.bulochka", *args], capture_output=True, text=True,
                              env=env, cwd=str(PRODUCT_ROOT), timeout=60)
