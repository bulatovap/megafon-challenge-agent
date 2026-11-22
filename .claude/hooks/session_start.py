#!/usr/bin/env python3
"""SessionStart[startup|resume|clear|compact]: сводка `bulochka status --for-hook` → additionalContext.

startup/resume/clear: операции прошлых сессий в статусе started → uncertain (процесс мёртв, автоповтора нет).
compact: дополнительно «потеряно при сжатии» из state/compact_gap.json и решения/ответы человека из state/.
"""
from __future__ import annotations

import json

import _common


def trust_hint():
    """Папка не принята в диалоге доверия → permissions.allow не действуют. Детект по ~/.claude.json
    (projects[<путь>].hasTrustDialogAccepted); путь к файлу можно переопределить BULOCHKA_CLAUDE_JSON."""
    import os
    from pathlib import Path
    cfg = Path(os.environ.get("BULOCHKA_CLAUDE_JSON") or (Path.home() / ".claude.json"))
    pd = os.environ.get("CLAUDE_PROJECT_DIR")
    if not pd or not cfg.is_file() or cfg.stat().st_size > 50_000_000:
        return None
    try:
        projects = json.loads(cfg.read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError):
        return None
    target = Path(pd).resolve()
    for key, val in projects.items():
        try:
            k = Path(key).resolve()
        except (OSError, ValueError):
            continue
        if k != target and k not in target.parents:
            continue
        if isinstance(val, dict) and val.get("hasTrustDialogAccepted"):
            return None
    return ("[bulochka] Папка проекта не отмечена как доверенная: разрешения из .claude/settings.json "
            "(permissions.allow) не действуют, команды bulochka будут спрашивать подтверждение. "
            "Запустите `claude` в этой папке интерактивно и примите доверие к папке.")


def handle(data):
    from tools.bulochka import core, journal, project
    source = data.get("source") or "startup"
    sid = data.get("session_id") or "unknown"
    s = _common.slug()
    if not s:
        ctx = ("[bulochka] Проекта ещё нет. Создайте: `python3 -m tools.bulochka new <slug> --brief <файл брифа>` "
               "и начните с /intake.")
        _common.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}})
        return 0

    sdir = core.state_dir(s)
    core.atomic_write_json(sdir / "session.json", {"session_id": sid, "source": source, "started_at": core.now_iso(),
                                                   "transcript_path": data.get("transcript_path")})
    marked = []
    if source != "compact":
        marked = journal.mark_session_uncertain(s, except_session=sid,
                                                reason="сессия завершилась, операция осталась started")
        journal.event("session.start", actor="hook", slug=s, session_id=sid, start_source=source)

    st = project.refresh_state(s)
    parts = []
    if source == "compact":
        gap_path = sdir / "compact_gap.json"
        if gap_path.exists():
            try:
                gap = json.loads(gap_path.read_text(encoding="utf-8"))
            except ValueError:
                gap = {}
            missing = gap.get("missing") or []
            if missing:
                parts.append("[bulochka] После сжатия в пересказе не было — восстанавливаю из state/: " + "; ".join(
                    f"{m.get('id')} — {m.get('what')}" for m in missing[:20]) + ".")
            else:
                parts.append("[bulochka] Сжатие проверено: ключевые записи state/ в пересказе есть.")
        # Решения и ответы человека — полностью, это то, что нельзя потерять
        dec = [d for d in st["decisions"]]
        if dec:
            parts.append("Решения (из decisions.jsonl): " + "; ".join(
                f"{d['id']} {d['status']}" + (f" → {d['chosen_option']}" if d.get('chosen_option') else "")
                + f": {d.get('question')}" for d in dec[:15]))
    if marked:
        parts.append(f"[bulochka] Прошлая сессия оборвалась: {', '.join(marked)} → uncertain. "
                     "Не повторять автоматически — показать человеку.")
    parts.append(project.for_hook_text(st))
    if source in ("startup", "resume"):
        hint = trust_hint()
        if hint:
            parts.append(hint)
    ctx = "\n".join(parts)
    if len(ctx) > 9000:
        ctx = ctx[:9000] + "\n…"
    _common.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}})
    return 0


if __name__ == "__main__":
    _common.run(handle)
