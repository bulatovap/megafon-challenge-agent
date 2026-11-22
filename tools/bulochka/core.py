"""Общие примитивы bulochka: пути, атомарная запись, JSONL-реестры, ID, время, ответ CLI.

Только стандартная библиотека, Python 3.9+. Все пути — абсолютные от корня проекта.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Коды выхода (контракт §4)
EXIT_OK = 0
EXIT_GATE_ERRORS = 1
EXIT_REFUSAL = 2
EXIT_USAGE = 3

# Префиксы ID и файлы реестров (контракт §0)
ENTITIES = {
    "source": ("S", "sources.jsonl", 3),
    "claim": ("C", "claims.jsonl", 3),
    "objection": ("R", "objections.jsonl", 3),
    "option": ("O", "options.jsonl", 3),
    "decision": ("D", "decisions.jsonl", 3),
    "refusal": ("X", "refusals.jsonl", 3),
    "gate": ("G", "gates.jsonl", 3),
    "journal": ("J", "journal.jsonl", 6),
}


class BulochkaError(Exception):
    """Ошибка использования CLI (exit 3)."""


# ---------- пути ----------

def root() -> Path:
    """Корень репозитория: env BULOCHKA_ROOT, иначе поиск вверх папки с tools/bulochka."""
    env = os.environ.get("BULOCHKA_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    # tools/bulochka/core.py -> корень на два уровня выше tools/
    return here.parent.parent.parent


def projects_dir() -> Path:
    return root() / "projects"


def current_slug() -> Optional[str]:
    env = os.environ.get("BULOCHKA_PROJECT")
    if env:
        return env
    f = projects_dir() / "CURRENT"
    if f.exists():
        slug = f.read_text(encoding="utf-8").strip()
        return slug or None
    return None


def set_current_slug(slug: str) -> None:
    atomic_write(projects_dir() / "CURRENT", slug + "\n")


def project_dir(slug: Optional[str] = None) -> Path:
    slug = slug or current_slug()
    if not slug:
        raise BulochkaError("Нет текущего проекта: создайте его командой `bulochka new <slug> --brief <file>`.")
    p = projects_dir() / slug
    if not p.exists():
        raise BulochkaError(f"Проект {slug} не найден в {projects_dir()}.")
    return p


def state_dir(slug: Optional[str] = None) -> Path:
    d = project_dir(slug) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(name: str) -> Path:
    return root() / "config" / name


def load_config(name: str, default: Any = None) -> Any:
    p = config_path(name)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- время и хэши ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Ключи отсортированы, без пробелов, строки NFC — для хэшей и ключей идемпотентности."""
    def norm(x: Any) -> Any:
        if isinstance(x, str):
            return nfc(x)
        if isinstance(x, dict):
            return {nfc(str(k)): norm(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [norm(v) for v in x]
        if isinstance(x, float) and x.is_integer():
            return int(x)
        return x
    return json.dumps(norm(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------- запись ----------

def atomic_write(path: Path, text: str) -> None:
    """Временный файл в той же папке → fsync → os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append-only: одна строка JSON, O_APPEND + fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # битую строку не роняем — её покажет doctor
                out.append({"_corrupt_line": n})
    return out


# ---------- реестры сущностей ----------

def entity_path(entity: str, slug: Optional[str] = None) -> Path:
    _, fname, _ = ENTITIES[entity]
    return state_dir(slug) / fname


def latest(entity: str, slug: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Текущее состояние: последняя строка с тем же id."""
    cur: Dict[str, Dict[str, Any]] = {}
    for rec in read_jsonl(entity_path(entity, slug)):
        rid = rec.get("id")
        if rid:
            cur[rid] = rec
    return cur


def get(entity: str, rid: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return latest(entity, slug).get(rid)


def next_id(entity: str, slug: Optional[str] = None) -> str:
    prefix, _, width = ENTITIES[entity]
    mx = 0
    for rid in latest(entity, slug):
        tail = rid[len(prefix):]
        if rid.startswith(prefix) and tail.isdigit():
            mx = max(mx, int(tail))
    return f"{prefix}{mx + 1:0{width}d}"


def put(entity: str, record: Dict[str, Any], slug: Optional[str] = None) -> Dict[str, Any]:
    """Дописать новую версию записи (append-only). Проставляет updated_at."""
    if "id" not in record:
        record["id"] = next_id(entity, slug)
    record.setdefault("created_at", now_iso())
    record["updated_at"] = now_iso()
    append_jsonl(entity_path(entity, slug), record)
    return record


def resolve_ref(rid: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Найти запись по любому ID (S/C/R/O/D/X/G)."""
    for entity, (prefix, _, _) in ENTITIES.items():
        if entity != "journal" and rid.startswith(prefix):
            return get(entity, rid, slug)
    return None


# ---------- пороги revise_if / revive_if ----------
#
# Семантика (контракт, замечание 9):
#   revise_if[].threshold — граница, при которой решение ДЕРЖИТСЯ: «>0,30» = решение в силе, пока показатель > 0,30;
#                           условие не выполнено → пора пересматривать;
#   revive_if[].threshold — условие ВОЗВРАТА отклонённого варианта: «<0,30» = вернуть, когда показатель < 0,30.
# Значение нормализуется как numbers.extract: «30%» → 0.3 (доля); «5 п.п.» → 0.05 (доля); «1,5 млн ₽» → 1500000;
# десятичная запятая, неразрывные пробелы, знак минус. Без оператора — «≥».

THRESHOLD_OPS = (
    (r"не\s+(?:ниже|меньше)|не\s+менее|минимум|от\b|>=|=>|≥|⩾", "≥"),
    (r"не\s+(?:выше|больше)|не\s+более|максимум|до\b|<=|=<|≤|⩽", "≤"),
    (r"выше|больше|более|>", ">"),
    (r"ниже|меньше|менее|<", "<"),
    (r"==|=|равно", "="),
)
_THR_SIGN = re.compile(r"^\s*([-−–])\s*")


def parse_threshold(s: Any) -> Tuple[str, float]:
    """Порог «>0,30», «≥ 35%», «не ниже 2 п.п.», «< 1,5 млн ₽», «-3 п.п.» → (оператор, значение).

    Оператор — один из > < ≥ ≤ =. Значение — float в единицах numbers.extract (проценты и п.п. — долей).
    Число без оператора → «≥». Порог без числа → BulochkaError."""
    from . import numbers
    if isinstance(s, bool) or s is None:
        raise BulochkaError(f"Порог не задан: {s!r}.")
    if isinstance(s, (int, float)):
        return "≥", float(s)
    text = numbers.clean(str(s)).strip()
    op = None
    for pat, sym in THRESHOLD_OPS:
        m = re.match(r"\s*(?:" + pat + r")\s*", text, re.IGNORECASE)
        if m:
            op, text = sym, text[m.end():]
            break
    sign = 1.0
    m = _THR_SIGN.match(text)
    if m:
        sign, text = -1.0, text[m.end():]
    nums = [n for n in numbers.extract(text) if n.get("unit") != "year"] or numbers.extract(text)
    if not nums:
        raise BulochkaError(f"Порог «{s}»: нет числа (ожидается, например, «>0,30», «≥ 35%», «< 2 п.п.»).")
    n = nums[0]
    value = float(n["value"])
    if n.get("unit") == "pp":
        value = value / 100.0
    return (op or "≥"), round(sign * value, 12)


def threshold_holds(op: str, value: float, actual: float) -> bool:
    """Выполнено ли «actual op value» (для revise_if — решение держится, для revive_if — пора вернуть)."""
    return {">": actual > value, "<": actual < value, "≥": actual >= value, "≤": actual <= value,
            "=": abs(actual - value) <= 1e-9}[op]


NEG_OP = {">": "≤", "<": "≥", "≥": "<", "≤": ">"}


def split_threshold(s: Any) -> Tuple[str, str]:
    """«≥ 50%» → («≥», «50%»): оператор (без оператора — «≥») и текст значения как написан."""
    text = str(s).strip()
    for pat, sym in THRESHOLD_OPS:
        m = re.match(r"\s*(?:" + pat + r")\s*", text, re.IGNORECASE)
        if m:
            return sym, text[m.end():].strip()
    return "≥", text


def holds_from_trigger(trigger: Any) -> str:
    """Условие пересмотра «≥ 50%» → граница, при которой решение держится: «< 50%»."""
    op, rest = split_threshold(trigger)
    if op not in NEG_OP:
        raise BulochkaError(f"revise_when «{trigger}»: нужен оператор >, <, ≥ или ≤ (например «≥ 50%»).")
    out = f"{NEG_OP[op]}{rest}"
    parse_threshold(out)
    return out


# ---------- ответ CLI (контракт §4) ----------

_BARE = re.compile(r"(?<![\w./-])bulochka (?=[a-z])")


def full_cmd(text: Any) -> Any:
    """«`bulochka ddiff D002`» → «`python3 -m tools.bulochka ddiff D002`»: голая bulochka в Bash не запустится."""
    if isinstance(text, str):
        return _BARE.sub("python3 -m tools.bulochka ", text)
    if isinstance(text, list):
        return [full_cmd(x) for x in text]
    if isinstance(text, dict):
        return {k: full_cmd(v) for k, v in text.items()}
    return text


def emit(cmd: str, data: Any = None, *, ok: bool = True, errors: Optional[Iterable[Dict[str, Any]]] = None,
         refusal: Optional[Dict[str, Any]] = None, journal_id: Optional[str] = None, next_cmd: Optional[str] = None,
         human: Optional[str] = None, as_json: bool = False, code: Optional[int] = None) -> int:
    errors = full_cmd(list(errors or []))
    refusal = full_cmd(refusal) if refusal else refusal
    human, next_cmd = full_cmd(human), full_cmd(next_cmd)
    payload = {"ok": ok, "cmd": cmd, "data": data if data is not None else {}, "errors": errors,
               "refusal": refusal, "journal_id": journal_id, "next": next_cmd}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if human:
            print(human)
        if refusal and refusal.get("phrase"):
            print(refusal["phrase"])
        for e in errors:
            print(f"[{e.get('level', 'error')}] {e.get('rule', '')} {e.get('msg', '')}".strip())
        if next_cmd:
            print(f"▶ Далее: {next_cmd}")
    if code is not None:
        return code
    if refusal:
        return EXIT_REFUSAL
    if any(e.get("level") == "error" for e in errors):
        return EXIT_GATE_ERRORS
    return EXIT_OK


def die_usage(msg: str) -> "None":
    print(msg, file=sys.stderr)
    sys.exit(EXIT_USAGE)
