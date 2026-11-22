"""bulochka doctor — проверка среды: Claude Code, Python, хуки, путь, целостность реестров, журнал.

Статусы проверок: ok, warn, fail. Код выхода 1, если есть хотя бы один fail.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core

HOOK_FILES = ["session_start.py", "session_end.py", "post_compact.py", "guard_write.py", "guard_web.py",
              "post_tool.py", "budget.py", "prompt_context.py", "stop_receipt.py", "statusline.py"]
FRESH_DAYS = 7


def _check(name: str, status: str, msg: str, **extra: Any) -> Dict[str, Any]:
    d = {"check": name, "status": status, "msg": msg}
    d.update(extra)
    return d


def check_claude() -> Dict[str, Any]:
    exe = shutil.which("claude")
    if not exe:
        return _check("claude", "warn", "claude не найден в PATH — агент работает только в Claude Code")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        v = (r.stdout or r.stderr).strip().splitlines()
        return _check("claude", "ok" if r.returncode == 0 else "warn", v[0] if v else "версия не получена")
    except (OSError, subprocess.SubprocessError) as e:
        return _check("claude", "warn", f"claude --version не ответил: {e}")


def check_python() -> List[Dict[str, Any]]:
    out = []
    vi = sys.version_info
    out.append(_check("python", "ok" if vi >= (3, 9) else "fail",
                      f"{sys.executable}: {vi.major}.{vi.minor}.{vi.micro}" + ("" if vi >= (3, 9) else " — нужен ≥3.9")))
    exe = shutil.which("python3")
    if exe and Path(exe).resolve() != Path(sys.executable).resolve():
        try:
            r = subprocess.run([exe, "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"],
                               capture_output=True, text=True, timeout=15)
            ver = r.stdout.strip()
            parts = tuple(int(x) for x in ver.split(".")[:2]) if ver else (0, 0)
            out.append(_check("python3_in_path", "ok" if parts >= (3, 9) else "fail",
                              f"хуки вызывают python3 = {exe} ({ver or '?'})"))
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            out.append(_check("python3_in_path", "warn", f"python3 в PATH не ответил: {e}"))
    elif not exe:
        out.append(_check("python3_in_path", "fail", "python3 не найден в PATH — хуки не запустятся"))
    return out


def check_path() -> Dict[str, Any]:
    r = core.root()
    s = str(r)
    notes = []
    if re.search(r"[^\x00-\x7f]", s):
        notes.append("кириллица/не-ASCII")
    if " " in s:
        notes.append("пробелы")
    # Реальная проверка: запись и чтение файла в этой папке
    try:
        probe = r / "projects" / ".doctor-проба тест.txt"
        core.atomic_write(probe, "проверка\n")
        ok = probe.read_text(encoding="utf-8") == "проверка\n"
        probe.unlink()
    except OSError as e:
        return _check("path", "fail", f"{s}: запись не удалась ({e})")
    if not ok:
        return _check("path", "fail", f"{s}: прочитано не то, что записано")
    msg = f"{s}" + (f" — {', '.join(notes)}; запись и чтение работают" if notes else "")
    return _check("path", "ok", msg, non_ascii="кириллица/не-ASCII" in notes, spaces="пробелы" in notes)


def check_settings() -> Dict[str, Any]:
    p = core.root() / ".claude" / "settings.json"
    if not p.exists():
        return _check("settings", "fail", ".claude/settings.json не найден — хуки и строка статуса не подключены")
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        return _check("settings", "fail", f".claude/settings.json не читается: {e}")
    missing = [h for h in HOOK_FILES if not (core.root() / ".claude" / "hooks" / h).exists()]
    if missing:
        return _check("settings", "warn", "нет файлов хуков: " + ", ".join(missing), missing=missing)
    return _check("settings", "ok", f"settings.json и {len(HOOK_FILES)} хуков на месте")


def _recent(ts: Any, days: int = FRESH_DAYS) -> bool:
    if not isinstance(ts, str):
        return False
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - t < timedelta(days=days)


def check_hooks_fired(slug: Optional[str]) -> Dict[str, Any]:
    if not slug:
        return _check("hooks_fired", "warn", "нет проекта: хукам пока нечего записывать")
    sd = core.state_dir(slug)
    evidence = []
    for r in core.read_jsonl(sd / "journal.jsonl"):
        if r.get("actor") == "hook" and _recent(r.get("ts")):
            evidence.append("журнал")
            break
    for r in core.read_jsonl(sd / "incidents.jsonl"):
        if _recent(r.get("ts")):
            evidence.append("инциденты")
            break
    try:
        c = json.loads((sd / "cost.json").read_text(encoding="utf-8"))
        if c.get("writer") in ("statusline", "budget") and _recent(c.get("ts")):
            evidence.append(f"cost.json ({c['writer']})")
    except (OSError, ValueError):
        pass
    if (core.project_dir(slug) / "STOP_RECEIPT.md").exists():
        evidence.append("STOP_RECEIPT.md")
    if evidence:
        return _check("hooks_fired", "ok", "хуки срабатывали: " + ", ".join(evidence))
    return _check("hooks_fired", "warn",
                  "свежих следов хуков нет: проверьте, что папке дано доверие (/hooks в Claude Code) и python3 доступен")


def check_jsonl(slug: Optional[str]) -> Dict[str, Any]:
    if not slug:
        return _check("jsonl", "ok", "нет проекта: реестров нет")
    bad = []
    n_files = 0
    for p in sorted(core.state_dir(slug).glob("*.jsonl")):
        n_files += 1
        with p.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("не объект")
                except ValueError:
                    bad.append(f"{p.name}:{i}")
    if bad:
        return _check("jsonl", "fail", "битые строки: " + ", ".join(bad[:10]) + (" …" if len(bad) > 10 else ""),
                      bad=bad)
    return _check("jsonl", "ok", f"{n_files} реестров читаются построчно")


def check_committed_files(slug: Optional[str]) -> Dict[str, Any]:
    if not slug:
        return _check("journal_files", "ok", "нет проекта")
    pdir = core.project_dir(slug)
    last: Dict[str, Dict[str, Any]] = {}
    for r in core.read_jsonl(core.state_dir(slug) / "journal.jsonl"):
        k = r.get("key") or r.get("id")
        if k:
            last[k] = r
    missing = []
    for k, r in last.items():
        op = r.get("output_path")
        if r.get("status") == "committed" and op:
            p = Path(op)
            if not p.is_absolute():
                p = pdir / p
            if not p.exists():
                missing.append(f"{r.get('id', k)} → {op}")
    if missing:
        return _check("journal_files", "fail", "committed без файла: " + "; ".join(missing[:10]), missing=missing)
    return _check("journal_files", "ok", "у всех committed-записей файл на месте")


def run_checks(with_claude: bool = True) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    if with_claude:
        checks.append(check_claude())
    checks.extend(check_python())
    checks.append(check_path())
    checks.append(check_settings())
    slug = core.current_slug()
    if slug and not (core.projects_dir() / slug).exists():
        checks.append(_check("project", "fail", f"projects/CURRENT указывает на {slug}, а папки нет"))
        slug = None
    checks.append(check_hooks_fired(slug))
    checks.append(check_jsonl(slug))
    checks.append(check_committed_files(slug))
    return checks



def render(checks: List[Dict[str, Any]]) -> str:
    """Чек готовности: позиции в порядке «где → чем → что считаем», статус справа, итог — готовность к смете."""
    by = {c["check"]: c for c in checks}
    vi = sys.version_info
    W = 46
    rows: List[tuple] = [("папка", core.root().name, by["path"]["status"]),
                         ("хуки", f"{len(HOOK_FILES)} шт.", by["settings"]["status"]),
                         ("python", f"{vi.major}.{vi.minor}.{vi.micro}", by["python"]["status"])]
    if "python3_in_path" in by and by["python3_in_path"]["status"] != "ok":
        rows.append(("python3", by["python3_in_path"]["msg"], by["python3_in_path"]["status"]))
    if "claude" in by:
        m = re.search(r"\d+\.\d+\.\d+", by["claude"]["msg"])
        rows.append(("claude", m.group(0) if m else by["claude"]["msg"], by["claude"]["status"]))
    slug = core.current_slug()
    if not slug:
        rows.append(("проект", "нет, смета после первого запроса", "ok"))
    else:
        rows.append(("проект", slug, "ok"))
        for k, name in (("hooks_fired", "следы"), ("jsonl", "реестры"), ("journal_files", "журнал")):
            if k in by:
                rows.append((name, by[k]["msg"], by[k]["status"]))
    st = {"ok": "OK", "warn": "?", "fail": "НЕТ"}
    out = ["ЧЕК ГОТОВНОСТИ · bulochka", "=" * W]
    for name, val, s in rows:
        left = f"{name:<8}{val}"
        out.append(left + " " * max(1, W - len(left) - len(st[s])) + st[s])
    out.append("-" * W)
    fails = any(c["status"] == "fail" for c in checks)
    out.append("Есть НЕТ: смету считать рано." if fails else "Готово к смете.")
    return "\n".join(out)


def cmd_doctor(args) -> int:
    checks = run_checks(with_claude=not args.no_claude)
    fails = [c for c in checks if c["status"] == "fail"]
    human = render(checks)
    errors = [{"rule": c["check"], "level": "error", "msg": c["msg"]} for c in fails]
    return core.emit("doctor", {"checks": checks, "ok": not fails}, ok=not fails, errors=errors if args.json else None,
                     human=human, as_json=args.json, code=core.EXIT_GATE_ERRORS if fails else core.EXIT_OK)


def register(sub) -> None:
    p = sub.add_parser("doctor", help="проверка среды: Claude Code, Python, хуки, путь, реестры")
    p.add_argument("--no-claude", action="store_true", help="не вызывать claude --version")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="машиночитаемый ответ")
    p.set_defaults(func=cmd_doctor)
