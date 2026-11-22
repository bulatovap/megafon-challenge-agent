"""Общая обвязка тестов среза «состояние»: временный корень данных, CLI только своих модулей, запуск хуков."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from tools.bulochka import core, gates, journal, project  # noqa: E402
from _isolate import isolate_ext  # noqa: E402

HOOKS = PRODUCT_ROOT / ".claude" / "hooks"
ENV_KEYS = ("BULOCHKA_ROOT", "BULOCHKA_PROJECT", "BULOCHKA_ACTOR", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR",
            "BULOCHKA_HOOK_NO_CHECK")


class StateCase(unittest.TestCase):
    def setUp(self):
        isolate_ext(self)  # расширения x_*.py и ext-хуки копии не влияют на тесты базового слоя
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        # пробел и кириллица в пути — как у жюри
        self.tmp = Path(tempfile.mkdtemp(prefix="bulochka тест-"))
        os.environ["BULOCHKA_ROOT"] = str(self.tmp)
        os.environ["BULOCHKA_ACTOR"] = "agent"
        os.environ["BULOCHKA_HOOK_NO_CHECK"] = "1"
        for k in ("BULOCHKA_PROJECT", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)
        (self.tmp / "projects").mkdir()
        (self.tmp / "config").mkdir()
        self.brief = self.tmp / "brief-in.md"
        self.brief.write_text("# Бриф\nОтток после триала подписки.\n", encoding="utf-8")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- CLI только наших модулей (чужие срезы могут быть в работе) ---
    def cli(self, *argv):
        p = argparse.ArgumentParser()
        p.add_argument("--json", action="store_true")
        p.add_argument("--project")
        sub = p.add_subparsers(dest="cmd")
        for m in (project, journal, gates):
            m.register(sub)
        args = p.parse_args(["--json"] + list(argv))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                code = int(args.func(args) or 0)
            except core.BulochkaError as e:
                code = core.emit(args.cmd or "", ok=False, errors=[{"rule": "USAGE", "level": "error", "msg": str(e)}],
                                 as_json=True, code=core.EXIT_USAGE)
        out = buf.getvalue().strip().splitlines()
        return code, (json.loads(out[-1]) if out else {})

    def new(self, slug="demo"):
        code, res = self.cli("new", slug, "--brief", str(self.brief))
        self.assertEqual(code, 0, res)
        return self.tmp / "projects" / slug

    def out(self, rel, text="x"):
        p = self.tmp / "projects" / core.current_slug() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return rel

    def finish_step(self, step):
        st, rec = journal.begin("step.run", {"step": step})
        self.assertEqual(st, "started")
        journal.commit(rec["id"], output=self.out(f"report/{step}.md"))

    def answer_gate(self, kind):
        g, _, _ = gates.ask(kind, f"Вопрос {kind}?", ["да", "нет"], why="меняет шаг", default="да")
        gates.answer(g["id"], "да")
        return g["id"]

    def hook(self, name, payload, raw=None):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        r = subprocess.run([sys.executable, str(HOOKS / name)],
                           input=raw if raw is not None else json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, env=env, timeout=60)
        return r

    def hook_json(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def incidents(self, slug="demo"):
        return core.read_jsonl(self.tmp / "projects" / slug / "state" / "incidents.jsonl")
