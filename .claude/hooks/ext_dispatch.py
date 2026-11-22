#!/usr/bin/env python3
"""Диспетчер хуков-расширений: запускает `.claude/hooks/ext/*.py` по алфавиту с тем же stdin.

Регистрируется в settings.json после хука базового слоя на SessionStart, UserPromptSubmit, PreToolUse(Bash|Edit|Write),
PostToolUse(Bash|Edit|Write|WebFetch) и Stop. Каждый ext-хук сам смотрит `hook_event_name` и выходит, если событие
не его. Файлы `_*.py` (общие помощники) и `statusline_*.py` (сегменты строки статуса) не запускаются.

Правила сведения:
- первый блок останавливает цепочку и уходит наружу как есть: exit 2 (причина из stderr),
  `permissionDecision: "deny"` (PreToolUse) или `"decision": "block"` (UserPromptSubmit, PostToolUse, Stop);
- `additionalContext` всех ext-хуков склеивается; простой текст в stdout тоже идёт в контекст;
- `permissionDecision: "ask"` передаётся, если блока не было; `allow` игнорируется — расширение не снимает проверки;
- сбой ext-хука (исключение, таймаут, другой код выхода, битый JSON) → state/incidents.jsonl, exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import _common

EXT_DIR = Path(os.environ.get("BULOCHKA_EXT_HOOKS_DIR") or Path(__file__).resolve().parent / "ext")  # env — для тестов
TIMEOUT = int(os.environ.get("BULOCHKA_EXT_HOOK_TIMEOUT") or 30)
CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")


def scripts() -> List[Path]:
    if not EXT_DIR.is_dir():
        return []
    return sorted(p for p in EXT_DIR.glob("*.py")
                  if p.is_file() and not p.name.startswith("_") and not p.name.startswith("statusline_"))


def parse_out(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def dispatch(raw: str, data: Dict[str, Any]) -> int:
    event = data.get("hook_event_name") or ""
    contexts: List[str] = []
    ask: Optional[Dict[str, Any]] = None
    system_msgs: List[str] = []
    for path in scripts():
        try:
            r = subprocess.run([sys.executable, str(path)], input=raw, capture_output=True, text=True,
                               timeout=TIMEOUT, cwd=data.get("cwd") or None, env=dict(os.environ))
        except subprocess.TimeoutExpired:
            _common.incident("ext_hook_error", f"{path.name}: таймаут {TIMEOUT} с", data, ext=path.name)
            continue
        except Exception as e:  # noqa: BLE001
            _common.incident("ext_hook_error", f"{path.name}: {type(e).__name__}: {e}", data, ext=path.name)
            continue
        if r.returncode == 2:
            reason = (r.stderr or "").strip() or f"заблокировано расширением {path.name}"
            sys.stderr.write(reason + "\n")
            return 2
        if r.returncode != 0:
            _common.incident("ext_hook_error", f"{path.name}: exit {r.returncode}: {(r.stderr or '')[-1500:]}",
                             data, ext=path.name)
            continue
        out = parse_out(r.stdout)
        if out is None:
            text = (r.stdout or "").strip()
            if text.startswith("{"):
                _common.incident("ext_hook_error", f"{path.name}: невалидный JSON в stdout", data, ext=path.name)
            elif text:
                contexts.append(text)
            continue
        hso = out.get("hookSpecificOutput") or {}
        if not isinstance(hso, dict):
            hso = {}
        decision = hso.get("permissionDecision")
        if decision == "deny" or out.get("decision") == "block" or out.get("continue") is False:
            if not hso.get("hookEventName") and hso:
                hso["hookEventName"] = event
            _common.emit(out)
            return 0
        if decision == "ask" and ask is None:
            ask = hso
        if hso.get("additionalContext"):
            contexts.append(str(hso["additionalContext"]))
        if out.get("systemMessage"):
            system_msgs.append(str(out["systemMessage"]))
    result: Dict[str, Any] = {}
    hso_out: Dict[str, Any] = {}
    if ask is not None and event == "PreToolUse":
        hso_out.update({"permissionDecision": "ask",
                        "permissionDecisionReason": ask.get("permissionDecisionReason") or ""})
    if contexts and event in CONTEXT_EVENTS:
        hso_out["additionalContext"] = "\n\n".join(contexts)
    if hso_out:
        hso_out["hookEventName"] = event
        result["hookSpecificOutput"] = hso_out
    if system_msgs:
        result["systemMessage"] = "\n".join(system_msgs)
    if result:
        _common.emit(result)
    return 0


def handle(data: Dict[str, Any]) -> int:
    return dispatch(RAW, data)


RAW = ""

if __name__ == "__main__":
    try:
        RAW = sys.stdin.read()
    except Exception:  # noqa: BLE001
        RAW = ""
    if not scripts():
        sys.exit(0)
    # _common.run читает stdin сам — подменяем его уже прочитанным текстом
    import io
    sys.stdin = io.StringIO(RAW)
    _common.run(handle)
