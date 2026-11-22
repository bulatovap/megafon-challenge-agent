"""Общее для хуков: вход JSON со stdin, пути от $CLAUDE_PROJECT_DIR, запись инцидентов, обёртка без traceback.

Правило: внутренняя ошибка хука → exit 0 + строка в state/incidents.jsonl. Блок — только намеренный (exit 2).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HOOKS_DIR = Path(__file__).resolve().parent
CODE_ROOT = HOOKS_DIR.parent.parent  # .claude/hooks → корень репо (там tools/bulochka)


def setup() -> None:
    """Импорт tools.bulochka из корня репо; данные — от BULOCHKA_ROOT, иначе $CLAUDE_PROJECT_DIR, иначе корень кода."""
    if str(CODE_ROOT) not in sys.path:
        sys.path.insert(0, str(CODE_ROOT))
    if not os.environ.get("BULOCHKA_ROOT"):
        pd = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["BULOCHKA_ROOT"] = str(Path(pd).resolve()) if pd else str(CODE_ROOT)


def data_root() -> Path:
    setup()
    return Path(os.environ["BULOCHKA_ROOT"])


def read_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def slug() -> Optional[str]:
    """Текущий проект, если он есть на диске."""
    from tools.bulochka import core
    s = core.current_slug()
    if s and (core.projects_dir() / s).is_dir():
        return s
    return None


def incident(kind: str, detail: str, data: Optional[Dict[str, Any]] = None, **extra: Any) -> None:
    """Пишет в projects/<slug>/state/incidents.jsonl (без проекта — в projects/incidents.jsonl). Никогда не падает."""
    try:
        setup()
        from tools.bulochka import core
        s = slug()
        path = (core.projects_dir() / s / "state" / "incidents.jsonl") if s else (core.projects_dir() / "incidents.jsonl")
        rec = {"ts": core.now_iso(), "kind": kind, "detail": detail[:2000],
               "hook": Path(sys.argv[0]).name, "event": (data or {}).get("hook_event_name"),
               "session_id": (data or {}).get("session_id"), "tool": (data or {}).get("tool_name")}
        rec.update(extra)
        core.append_jsonl(path, rec)
    except Exception:  # noqa: BLE001 — инцидент не должен ронять хук
        pass


def emit(obj: Dict[str, Any]) -> None:
    """Ответ хука в stdout. Подсказки «`bulochka …`» — в полной форме: голая bulochka в Bash не запустится."""
    try:
        setup()
        from tools.bulochka import core
        obj = core.full_cmd(obj)
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def block(reason: str) -> int:
    """PreToolUse-блок: причина в stderr, exit 2."""
    sys.stderr.write(reason.rstrip() + "\n")
    return 2


def run(handler: Callable[[Dict[str, Any]], Optional[int]]) -> None:
    data: Dict[str, Any] = {}
    code = 0
    try:
        setup()
        data = read_input()
        if data.get("session_id") and not os.environ.get("CLAUDE_SESSION_ID"):
            os.environ["CLAUDE_SESSION_ID"] = str(data["session_id"])
        code = int(handler(data) or 0)
    except SystemExit as e:  # намеренный sys.exit внутри обработчика
        code = int(e.code or 0) if isinstance(e.code, int) else 0
    except BaseException as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=6)
        incident("hook_error", f"{type(e).__name__}: {e}\n{tb}", data)
        code = 0
    sys.stdout.flush()
    sys.exit(code if code in (0, 2) else 0)


def project_rel(path: str, cwd: Optional[str] = None) -> Path:
    """Абсолютный путь файла из tool_input (относительный — от cwd хука, иначе от корня данных)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or data_root()) / p
    try:
        return p.resolve()
    except OSError:
        return p
