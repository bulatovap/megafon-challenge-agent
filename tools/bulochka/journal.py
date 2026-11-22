"""Журнал операций (контракт §1.8, §7): ключ идемпотентности, статусы started/committed/failed/uncertain.

Хранение: `projects/<slug>/state/journal.jsonl`, append-only. У одной операции (одного ключа) один id `J######`;
каждое изменение статуса — новая строка с тем же id и key. Состояние ключа — последняя строка.

Команды: `journal begin | commit | fail | resolve`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import core

STATUSES = ("started", "committed", "failed", "uncertain")
SIDE_EFFECTS = ("none", "file_write", "external")
ACTORS = ("agent", "human", "hook")

# Таймаут «started» по операции, секунды. Старше — переводится в uncertain при повторном begin.
DEFAULT_TIMEOUTS = {"source.fetch": 600, "step.run": 4 * 3600, "export": 900, "claim.add": 300}
DEFAULT_TIMEOUT = 1800
MAX_AUTO_RETRY = 3  # для side_effect=none: повтор после failed не больше 3 раз

# Не входят в ключ: иначе повтор не распознается (контракт §7)
VOLATILE_KEYS = {"ts", "time", "timestamp", "session_id", "actor", "created_at", "updated_at", "now"}
TRACKING_PARAM = re.compile(r"^(utm_[a-z_]+|fbclid|gclid|yclid|ysclid|_openstat|mc_cid|mc_eid|_ga|igshid|spm)$", re.I)


# ---------- ключ ----------

def normalize_url(u: str) -> str:
    """Схема и хост в нижнем регистре, без фрагмента и трекинговых параметров, параметры отсортированы."""
    try:
        parts = urlsplit(u.strip())
    except ValueError:
        return u.strip()
    if not parts.scheme or not parts.netloc:
        return u.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    query.sort()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def normalize_inputs(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): normalize_inputs(v) for k, v in obj.items() if str(k) not in VOLATILE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [normalize_inputs(v) for v in obj]
    if isinstance(obj, str):
        s = core.nfc(obj.strip())
        if re.match(r"^https?://", s, re.I):
            return normalize_url(s)
        return s
    return obj


def make_key(slug: str, operation: str, inputs: Any, source_version: Optional[str] = None) -> str:
    canon = core.canonical_json(normalize_inputs(inputs if inputs is not None else {}))
    raw = f"{slug}|{operation}|{canon}|{source_version or ''}"
    return "sha256:" + core.sha256_text(raw)


def inputs_hash(inputs: Any) -> str:
    return core.sha256_text(core.canonical_json(normalize_inputs(inputs if inputs is not None else {})))


# ---------- чтение ----------

def _path(slug: Optional[str] = None) -> Path:
    return core.entity_path("journal", slug)


def records(slug: Optional[str] = None) -> List[Dict[str, Any]]:
    return [r for r in core.read_jsonl(_path(slug)) if "_corrupt_line" not in r]


def by_key(slug: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Последняя запись по каждому ключу."""
    cur: Dict[str, Dict[str, Any]] = {}
    for r in records(slug):
        k = r.get("key")
        if k:
            cur[k] = r
    return cur


def find(ref: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Последняя запись по ключу `sha256:…` или по id `J######`."""
    found = None
    for r in records(slug):
        if r.get("key") == ref or r.get("id") == ref:
            found = r
    return found


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(rec: Dict[str, Any]) -> float:
    t = parse_ts(rec.get("ts"))
    if not t:
        return 0.0
    return (datetime.now(timezone.utc) - t).total_seconds()


def timeout_of(rec: Dict[str, Any]) -> int:
    return int(rec.get("timeout_s") or DEFAULT_TIMEOUTS.get(rec.get("operation", ""), DEFAULT_TIMEOUT))


def paused_at_gate(rec: Dict[str, Any], slug: Optional[str] = None) -> bool:
    """step.run, который ждёт человека: на этом шаге после начала задан вопрос (ворота).
    Такой шаг не «оборван», а на паузе — сессия могла закончиться, пока человек думает."""
    if rec.get("operation") != "step.run" or rec.get("status") != "started" or not rec.get("step"):
        return False
    since = rec.get("started_at") or rec.get("ts") or ""
    try:
        gates = core.latest("gate", slug).values()
    except Exception:  # noqa: BLE001
        return False
    return any(g.get("step") == rec["step"] and str(g.get("created_at") or "") >= since for g in gates)


def uncertain_list(slug: Optional[str] = None) -> List[Dict[str, Any]]:
    """Операции без автоповтора: статус uncertain и зависшие started (старше таймаута)."""
    out = []
    for r in by_key(slug).values():
        st = r.get("status")
        if st == "uncertain" or (st == "started" and age_seconds(r) >= timeout_of(r)
                                 and not paused_at_gate(r, slug)):
            out.append({"id": r.get("id"), "key": r.get("key"), "operation": r.get("operation"),
                        "step": r.get("step"), "status": st, "side_effect": r.get("side_effect"),
                        "ts": r.get("ts"), "reason": r.get("uncertain_reason")})
    return out


# ---------- запись ----------

@contextlib.contextmanager
def _lock(slug: Optional[str] = None):
    """Эксклюзивная блокировка журнала: параллельные субагенты не получат одинаковый J-id."""
    lock_path = core.state_dir(slug) / ".journal.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover — не POSIX
            pass
        yield
    finally:
        os.close(fd)


def _next_id(slug: Optional[str] = None) -> str:
    mx = 0
    for r in records(slug):
        rid = str(r.get("id", ""))
        if rid.startswith("J") and rid[1:].isdigit():
            mx = max(mx, int(rid[1:]))
    return f"J{mx + 1:06d}"


def _append(rec: Dict[str, Any], slug: Optional[str] = None) -> Dict[str, Any]:
    rec["ts"] = core.now_iso()
    core.append_jsonl(_path(slug), rec)
    return rec


def _transition(last: Dict[str, Any], status: str, slug: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    rec = {k: v for k, v in last.items() if k not in ("ts", "error", "uncertain_reason")}
    rec["status"] = status
    rec.update({k: v for k, v in fields.items() if v is not None})
    return _append(rec, slug)


def current_session_id(slug: Optional[str] = None) -> str:
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        return sid
    try:
        p = core.state_dir(slug) / "session.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("session_id") or "cli"
    except (OSError, ValueError, core.BulochkaError):
        pass
    return "cli"


def resolve_output(output: str, slug: Optional[str] = None) -> Tuple[Path, str]:
    """Абсолютный путь и путь для записи (относительно папки проекта, если внутри неё)."""
    pdir = core.project_dir(slug)
    p = Path(output)
    if not p.is_absolute():
        # skills пишут путь от корня («projects/<slug>/report/…»), люди — от проекта («report/…»): берём тот,
        # что существует; иначе путь, начинающийся с projects/, — от корня (без удвоения projects/…/projects/…)
        from_project, from_root = pdir / p, core.projects_dir().parent / p
        if from_project.exists():
            p = from_project
        elif from_root.exists() or (p.parts and p.parts[0] == "projects"):
            p = from_root
        else:
            p = from_project
    p = p.resolve()
    try:
        rel = str(p.relative_to(pdir.resolve()))
    except ValueError:
        rel = str(p)
    return p, rel


def begin(operation: str, inputs: Any, *, side_effect: str = "none", step: Optional[str] = None,
          actor: str = "agent", source_version: Optional[str] = None, timeout_s: Optional[int] = None,
          session_id: Optional[str] = None, slug: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Возвращает (status, record). status ∈ started | skip_done | skip_running | uncertain | retry_limit."""
    slug = slug or core.current_slug()
    core.project_dir(slug)
    if side_effect not in SIDE_EFFECTS:
        raise core.BulochkaError(f"--side-effect: одно из {', '.join(SIDE_EFFECTS)}")
    key = make_key(slug, operation, inputs, source_version)
    if step is None and isinstance(inputs, dict) and isinstance(inputs.get("step"), str):
        step = inputs["step"]
    with _lock(slug):
        last = by_key(slug).get(key)
        if last is None:
            rec = {"id": _next_id(slug), "key": key, "operation": operation, "step": step or _current_step(slug),
                   "status": "started", "side_effect": side_effect, "inputs_hash": inputs_hash(inputs),
                   "source_version": source_version, "output_path": None, "actor": actor,
                   "session_id": session_id or current_session_id(slug), "attempt": 1,
                   "started_at": core.now_iso()}
            if timeout_s:
                rec["timeout_s"] = int(timeout_s)
            norm = normalize_inputs(inputs)
            if len(core.canonical_json(norm)) <= 500:
                rec["inputs"] = norm
            rec = _append(rec, slug)
            if operation == "step.run":
                _on_step_begin(rec.get("step"), slug)
            return "started", rec
        st = last.get("status")
        if st == "committed":
            return "skip_done", last
        if st == "started" and paused_at_gate(last, slug):
            # шаг ждал ответа человека на воротах — продолжаем ту же запись в новой сессии
            sid = session_id or current_session_id(slug)
            if last.get("session_id") != sid:
                last = _transition(last, "started", slug, session_id=sid, actor=actor)
            return "resumed", last
        if st == "started":
            limit = int(timeout_s if timeout_s is not None else timeout_of(last))
            if age_seconds(last) < limit:
                return "skip_running", last
            rec = _transition(last, "uncertain", slug, uncertain_reason=f"started старше таймаута {limit} с")
            return "uncertain", rec
        if st == "uncertain":
            return "uncertain", last
        # failed → повтор разрешён
        attempt = int(last.get("attempt") or 1) + 1
        if last.get("side_effect") == "none" and actor == "agent" and attempt > MAX_AUTO_RETRY + 1:
            return "retry_limit", last
        rec = _transition(last, "started", slug, attempt=attempt, actor=actor,
                          session_id=session_id or current_session_id(slug))
        if operation == "step.run":
            _on_step_begin(rec.get("step"), slug)
        rec["retry_after_s"] = 2 ** (attempt - 1) if last.get("side_effect") == "none" else 0
        return "started", rec


def _must_find(ref: str, slug: Optional[str]) -> Dict[str, Any]:
    last = find(ref, slug)
    if last is None:
        raise core.BulochkaError(f"В журнале нет операции {ref}.")
    return last


def commit(ref: str, *, output: Optional[str] = None, tokens: Optional[Dict[str, Any]] = None,
           slug: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    slug = slug or core.current_slug()
    with _lock(slug):
        last = _must_find(ref, slug)
        st = last.get("status")
        if st == "committed":
            return "committed", last
        if st != "started":
            raise core.BulochkaError(
                f"{last['id']} в статусе {st}: commit возможен только после begin. "
                f"Для uncertain решение за человеком: journal resolve {last['id']} --as committed|failed.")
        out_rel = last.get("output_path")
        if output:
            p, out_rel = resolve_output(output, slug)
            if not p.exists():
                raise core.BulochkaError(f"Файл результата не найден: {p}. Сначала запишите его, потом commit.")
        rec = _transition(last, "committed", slug, output_path=out_rel, tokens=tokens)
    _after_change(slug)
    return "committed", rec


def fail(ref: str, *, error: Optional[str] = None, output: Optional[str] = None,
         slug: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """started → failed; если результат частично записан → uncertain.
    uncertain → failed разрешено агенту только для side_effect=none (контракт §7)."""
    slug = slug or core.current_slug()
    with _lock(slug):
        last = _must_find(ref, slug)
        st = last.get("status")
        if st == "failed":
            return "failed", last
        if st == "committed":
            raise core.BulochkaError(f"{last['id']} уже committed — fail невозможен.")
        if st == "uncertain":
            if last.get("side_effect") != "none":
                return "refused", last
            rec = _transition(last, "failed", slug, error=error or "uncertain → failed (side_effect=none)")
            return "failed", rec
        out_rel = None
        partial = False
        if output:
            p, out_rel = resolve_output(output, slug)
            partial = p.exists() and p.stat().st_size > 0
        if partial:
            rec = _transition(last, "uncertain", slug, error=error, output_path=out_rel,
                              uncertain_reason="failed, но файл результата частично записан")
            status = "uncertain"
        else:
            rec = _transition(last, "failed", slug, error=error or "без описания", output_path=out_rel)
            status = "failed"
    _after_change(slug)
    return status, rec


def is_human() -> bool:
    """Человек: BULOCHKA_ACTOR=human (команда с префиксом env не попадает в allow — Claude Code спросит человека)
    или интерактивный терминал. Явный BULOCHKA_ACTOR=<другое> — не человек."""
    env = os.environ.get("BULOCHKA_ACTOR")
    if env:
        return env == "human"
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


_DONE = re.compile(r"заверш|готов|сделан|выполн|\bда\b|committed", re.IGNORECASE)
_NOT_DONE = re.compile(r"не\s+(?:заверш|готов|сделан|выполн)|\bнет\b|повтор|передела|failed", re.IGNORECASE)


def _gate_allows(gate_id: Optional[str], last: Dict[str, Any], as_status: str, slug: Optional[str]) -> bool:
    """Решение человека из чата: ворота с ответом человека, в вопросе назван этот J###, ответ совпадает по смыслу."""
    if not gate_id:
        return False
    g = core.get("gate", gate_id, slug) or {}
    if not g.get("answer") or g.get("answered_by") != "human":
        return False
    if last.get("id") not in str(g.get("question") or "") + " " + " ".join(map(str, g.get("options") or [])):
        return False
    ans = str(g["answer"])
    if as_status == "failed":
        return bool(_NOT_DONE.search(ans))
    return bool(_DONE.search(ans)) and not _NOT_DONE.search(ans)


def resolve(ref: str, as_status: str, *, output: Optional[str] = None, note: Optional[str] = None,
            slug: Optional[str] = None, gate: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Человек: uncertain/started → committed|failed — из терминала или по его ответу в чате на ворота (--gate),
    где назван этот J###."""
    slug = slug or core.current_slug()
    if as_status not in ("committed", "failed"):
        raise core.BulochkaError("--as: committed или failed")
    if not is_human() and not _gate_allows(gate, _must_find(ref, slug), as_status, slug):
        return "refused", {}
    with _lock(slug):
        last = _must_find(ref, slug)
        st = last.get("status")
        if st not in ("uncertain", "started"):
            raise core.BulochkaError(f"{last['id']} в статусе {st}: resolve нужен только для uncertain.")
        out_rel = last.get("output_path")
        if output:
            _, out_rel = resolve_output(output, slug)
        if as_status == "committed" and out_rel:
            p, _ = resolve_output(out_rel, slug)
            if not p.exists():
                raise core.BulochkaError(f"committed без файла результата нельзя: {p} не найден.")
        rec = _transition(last, as_status, slug, actor="human", output_path=out_rel,
                          error=(note or "решено человеком") if as_status == "failed" else None,
                          resolution=note or f"человек: {as_status}")
    _after_change(slug)
    return as_status, rec


def event(operation: str, *, step: Optional[str] = None, actor: str = "hook", session_id: Optional[str] = None,
          slug: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    """Разовое событие (session.start/end, gate.answer, tool.write): сразу committed, уникальный ключ."""
    slug = slug or core.current_slug()
    with _lock(slug):
        rec = {"id": _next_id(slug), "key": "evt:" + uuid.uuid4().hex, "operation": operation,
               "step": step or _current_step(slug), "status": "committed", "side_effect": "none",
               "inputs_hash": inputs_hash({}), "source_version": None, "output_path": None, "actor": actor,
               "session_id": session_id or current_session_id(slug)}
        rec.update({k: v for k, v in fields.items() if v is not None})
        op = rec.get("output_path")
        if op and Path(str(op)).is_absolute():  # в журнале — путь от проекта или от корня, не абсолютный
            pp = Path(str(op)).resolve()
            for base in (core.project_dir(slug).resolve(), core.projects_dir().parent.resolve()):
                try:
                    rec["output_path"] = str(pp.relative_to(base))
                    break
                except ValueError:
                    continue
        return _append(rec, slug)


def mark_session_uncertain(slug: Optional[str], *, only_session: Optional[str] = None,
                           except_session: Optional[str] = None, reason: str = "") -> List[str]:
    """started без завершения → uncertain. only_session: этой сессии (SessionEnd);
    except_session: всех, кроме текущей (SessionStart — прошлые сессии мертвы)."""
    changed = []
    with _lock(slug):
        for r in list(by_key(slug).values()):
            if r.get("status") != "started":
                continue
            sid = r.get("session_id")
            if only_session is not None and sid != only_session:
                continue
            if except_session is not None and sid == except_session:
                continue
            if paused_at_gate(r, slug):
                continue  # шаг ждёт человека на воротах — это пауза, а не обрыв
            _transition(r, "uncertain", slug, uncertain_reason=reason or "операция не завершилась")
            changed.append(r["id"])
    if changed:
        _after_change(slug)
    return changed


def _current_step(slug: Optional[str]) -> Optional[str]:
    try:
        from . import project
        return project.compute(slug).get("step")
    except Exception:  # noqa: BLE001 — шаг для журнала не критичен
        return None


def _on_step_begin(step: Optional[str], slug: Optional[str]) -> None:
    from . import project
    project.on_step_begin(step, slug)


def _after_change(slug: Optional[str]) -> None:
    try:
        from . import project
        project.refresh_state(slug)
    except Exception:  # noqa: BLE001 — кэш состояния пересоберётся при следующем вызове
        pass


# ---------- CLI ----------

def _parse_inputs(raw: Optional[str]) -> Any:
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise core.BulochkaError(f"--inputs должен быть JSON: {e}")


def cmd_begin(args) -> int:
    inputs = _parse_inputs(args.inputs)
    op = args.op
    if not op:
        if not args.step:
            raise core.BulochkaError("Нужен --op или --step.")
        op = "step.run"
    if args.step and isinstance(inputs, dict):
        inputs.setdefault("step", args.step)
    status, rec = begin(op, inputs, side_effect=args.side_effect, step=args.step, actor=args.actor,
                        source_version=args.source_version, timeout_s=args.timeout)
    data = {"key": rec.get("key"), "id": rec.get("id"), "status": status, "operation": op,
            "step": rec.get("step"), "attempt": rec.get("attempt")}
    if rec.get("retry_after_s"):
        data["retry_after_s"] = rec["retry_after_s"]
    human = {
        "started": f"{rec.get('id')} started: {op}. После записи результата: bulochka journal commit {rec.get('id')} --output <файл>",
        "resumed": f"{rec.get('id')} продолжается: шаг ждал ответа человека на воротах. Сделанное на шаге не повторяй; "
                   f"в конце: bulochka journal commit {rec.get('id')} --output <файл>",
        "skip_done": f"{rec.get('id')} уже committed ({rec.get('output_path') or 'без файла'}) — не повторяю.",
        "skip_running": f"{rec.get('id')} уже выполняется ({rec.get('ts')}) — не запускаю второй раз.",
        "uncertain": f"{rec.get('id')} в статусе uncertain: неизвестно, завершилась ли операция. Автоповтора нет — "
                     f"решает человек: bulochka journal resolve {rec.get('id')} --as committed|failed",
        "retry_limit": f"{rec.get('id')}: {MAX_AUTO_RETRY} повтора уже было — дальше решает человек.",
    }[status]
    code = core.EXIT_REFUSAL if status in ("uncertain", "retry_limit") else core.EXIT_OK
    return core.emit("journal begin", data, ok=code == 0, journal_id=rec.get("id"), human=human,
                     as_json=args.json, code=code)


def cmd_commit(args) -> int:
    tokens = _parse_inputs(args.tokens) if args.tokens else None
    status, rec = commit(args.key, output=args.output, tokens=tokens)
    return core.emit("journal commit", {"key": rec.get("key"), "id": rec.get("id"), "status": status,
                                        "output_path": rec.get("output_path")},
                     journal_id=rec.get("id"), human=f"{rec.get('id')} committed", as_json=args.json)


def cmd_fail(args) -> int:
    status, rec = fail(args.key, error=args.error, output=args.output)
    if status == "refused":
        return core.emit("journal fail", {"key": rec.get("key"), "id": rec.get("id"), "status": "uncertain"},
                         ok=False, journal_id=rec.get("id"), as_json=args.json, code=core.EXIT_REFUSAL,
                         human=f"{rec.get('id')} с внешним эффектом ({rec.get('side_effect')}) в статусе uncertain — "
                               f"переводит человек: bulochka journal resolve {rec.get('id')} --as committed|failed")
    return core.emit("journal fail", {"key": rec.get("key"), "id": rec.get("id"), "status": status},
                     journal_id=rec.get("id"), human=f"{rec.get('id')} {status}", as_json=args.json)


def cmd_resolve(args) -> int:
    status, rec = resolve(args.key, args.as_status, output=args.output, note=args.note,
                          gate=getattr(args, "gate", None))
    if status == "refused":
        return core.emit("journal resolve", {"key": args.key}, ok=False, as_json=args.json, code=core.EXIT_REFUSAL,
                         human="Перевести uncertain решает человек. Спроси его в чате: "
                               f"`bulochka ask --kind question --question \"{args.key}: шаг завершён или повторить?\" "
                               f"--options \"Завершён\" \"Повторить\"`, затем `bulochka journal resolve {args.key} "
                               f"--as committed|failed --gate G###` по его ответу (или он сам в терминале: "
                               f"`BULOCHKA_ACTOR=human python3 -m tools.bulochka journal resolve {args.key} --as …`).")
    return core.emit("journal resolve", {"key": rec.get("key"), "id": rec.get("id"), "status": status},
                     journal_id=rec.get("id"), human=f"{rec.get('id')} → {status} (человек)", as_json=args.json)


def register(sub) -> None:
    p = sub.add_parser("journal", help="журнал операций: begin/commit/fail/resolve")
    jsub = p.add_subparsers(dest="journal_cmd")

    b = jsub.add_parser("begin", help="начать операцию (идемпотентно)")
    b.add_argument("--op", help="операция: source.fetch, claim.add, step.run, export …")
    b.add_argument("--inputs", help="JSON входов операции")
    b.add_argument("--step", help="шаг workflow; без --op означает step.run")
    b.add_argument("--side-effect", dest="side_effect", default="none", choices=SIDE_EFFECTS)
    b.add_argument("--source-version", dest="source_version")
    b.add_argument("--actor", default="agent", choices=ACTORS)
    b.add_argument("--timeout", type=int, help="таймаут started, секунды")
    b.set_defaults(func=cmd_begin)

    c = jsub.add_parser("commit", help="операция завершена, результат записан")
    c.add_argument("key", help="ключ sha256:… или J######")
    c.add_argument("--output", help="файл результата (должен существовать)")
    c.add_argument("--tokens", help='JSON {"in","out","cache_read","cache_write"}')
    c.set_defaults(func=cmd_commit)

    f = jsub.add_parser("fail", help="операция не удалась")
    f.add_argument("key")
    f.add_argument("--error")
    f.add_argument("--output", help="файл результата, если мог быть частично записан")
    f.set_defaults(func=cmd_fail)

    r = jsub.add_parser("resolve", help="решение человека по uncertain")
    r.add_argument("--gate", help="G### — ответ человека в чате, где назван этот J###")
    r.add_argument("key")
    r.add_argument("--as", dest="as_status", required=True, choices=("committed", "failed"))
    r.add_argument("--output")
    r.add_argument("--note")
    r.set_defaults(func=cmd_resolve)

    def _help(args) -> int:
        p.print_help()
        return core.EXIT_USAGE
    p.set_defaults(func=_help)
