"""Общее для ext-хуков экономики: путь к коду, stdin, вывод. Файлы `_*.py` диспетчер не запускает."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
CODE_ROOT = HOOKS.parents[1]
for p in (str(CODE_ROOT), str(HOOKS)):
    if p not in sys.path:
        sys.path.insert(0, p)
if not os.environ.get("BULOCHKA_ROOT"):
    pd = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["BULOCHKA_ROOT"] = str(Path(pd).resolve()) if pd else str(CODE_ROOT)


def read_stdin():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def slug():
    from tools.bulochka import core
    s = core.current_slug()
    return s if s and (core.projects_dir() / s).is_dir() else None


def context(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                     ensure_ascii=False))


def incident(name, err):
    try:
        from tools.bulochka import core
        s = slug()
        if s:
            core.append_jsonl(core.state_dir(s) / "incidents.jsonl",
                              {"ts": core.now_iso(), "hook": name, "error": err})
    except Exception:  # noqa: BLE001
        pass
