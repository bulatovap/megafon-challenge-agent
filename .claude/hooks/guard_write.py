#!/usr/bin/env python3
"""PreToolUse[Edit|Write|NotebookEdit]: блок записи в .env/.env.*, secrets/, projects/*/state/, в файлы locked-решений
и за пределы корня проекта (разрешены только /tmp, scratchpad Claude Code и ~/.claude/plans).

Блок — exit 2, причина в stderr (её видит агент). Запись в projects/<slug>/inbox/ и interviews/synthetic/ разрешена.

Файл locked-решения — это файл с D### в имени или существующий ADR (`report/*approval*.md`, `report/*decision*.md`),
в тексте которого упомянуто решение со статусом locked. Новый файл ADR (его ещё нет на диске) писать можно.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import _common

DEC_ID = re.compile(r"(?<![A-Za-z0-9])(D\d{3,})(?!\d)")
ADR_NAME = re.compile(r"approval|decision", re.IGNORECASE)


def allowed_outside() -> list:
    """Каталоги вне проекта, куда запись разрешена: /tmp (на macOS это /private/tmp — там же scratchpad
    Claude Code), явный scratchpad из окружения и папка планов ~/.claude/plans."""
    dirs = [Path("/tmp"), Path("/private/tmp"), Path.home() / ".claude" / "plans"]
    for key in ("CLAUDE_SCRATCHPAD_DIR", "BULOCHKA_SCRATCHPAD"):
        if os.environ.get(key):
            dirs.append(Path(os.environ[key]))
    out = []
    for d in dirs:
        try:
            out.append(d.resolve())
        except OSError:
            out.append(d)
    return out


def _under(p: Path, base: Path) -> bool:
    return p == base or base in p.parents


def outside_root(p: Path):
    """Причина блока, если путь вне корня проекта и не во временной папке."""
    from tools.bulochka import core
    roots = {core.root().resolve(), _common.CODE_ROOT.resolve()}
    if any(_under(p, r) for r in roots):
        return None
    if any(_under(p, d) for d in allowed_outside()):
        return None
    return (f"Запись вне папки проекта заблокирована: {p}. Скрипты и черновики кладёшь внутрь проекта "
            "(например, projects/<slug>/inbox/) или во временную папку /tmp.")


def check_path(p: Path):
    """Причина блока или None."""
    from tools.bulochka import core
    name = p.name
    if name == ".env" or name.startswith(".env."):
        return (f"Запись в {name} заблокирована: секреты и ключи агент не пишет. "
                "Если нужна настройка — попросите человека.")
    if "secrets" in p.parts:
        return "Запись в secrets/ заблокирована: секреты агент не пишет."
    reason = outside_root(p)
    if reason:
        return reason
    projects = core.projects_dir().resolve()
    try:
        rel = p.relative_to(projects)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "state":
        return (f"Прямая запись в projects/{parts[0]}/state/{'/'.join(parts[2:])} заблокирована: реестры и журнал "
                "меняются только командами `python3 -m tools.bulochka …` (source/claim/objection/option/decision/"
                "ask/answer/journal).")
    if len(parts) >= 2:
        ids = set(DEC_ID.findall(name))
        if len(parts) >= 3 and parts[1] == "report" and ADR_NAME.search(name) and p.is_file():
            try:
                ids |= set(DEC_ID.findall(p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
        if ids:
            try:
                decisions = core.latest("decision", parts[0])
            except core.BulochkaError:
                decisions = {}
            locked = sorted(i for i in ids if decisions.get(i, {}).get("status") == "locked")
            if locked:
                return (f"Решение {', '.join(locked)} зафиксировано (locked): его файл не правится (E19). "
                        f"Пересмотр — `bulochka decision add --supersedes {locked[0]} --proposed` и новый файл ADR.")
    return None


def handle(data):
    ti = data.get("tool_input") or {}
    raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not raw:
        return 0
    p = _common.project_rel(str(raw), data.get("cwd"))
    reason = check_path(p)
    if reason:
        _common.incident("write_blocked", reason, data, path=str(p))
        try:
            from tools.bulochka import journal
            if _common.slug():
                journal.event("write.guard", actor="hook", session_id=data.get("session_id"), decision="deny",
                              reason=reason[:300], tool=data.get("tool_name"), target=str(p)[-300:],
                              agent_id=data.get("agent_id"), agent_type=data.get("agent_type"))
        except Exception:  # noqa: BLE001 — блок важнее записи
            pass
        return _common.block(reason)
    return 0


if __name__ == "__main__":
    _common.run(handle)
