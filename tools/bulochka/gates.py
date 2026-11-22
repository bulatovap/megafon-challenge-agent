"""Вопросы человеку (контракт §1.7): `ask` регистрирует ворота G###, `answer` записывает ответ и review_seconds.

Точки расширения `answer` (по образцу decisions.PRE_CHOOSE; модули x_*.py добавляют функции при импорте):
- PRE_ANSWER  — fn(gate, answer_text) -> отказ | None, до записи ответа (валидатор или обработчик, например снимок
  состояния перед ответом). Первый отказ останавливает запись: AnswerRefused, `bulochka answer` → exit 2.
  Сбой функции — строка в stderr, не блок. Повтор того же ответа на отвеченные ворота их не вызывает.
- POST_ANSWER — fn(gate) -> None, после записи ответа (gate — уже с answer). Сбой — stderr.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import core, ext

KINDS = ("brief_approval", "choose_option", "approve_decision", "question")
MAX_CLAIMS_SHOWN = 7
MAX_INTAKE_QUESTIONS = 3
MIN_OPTIONS, MAX_OPTIONS = 2, 8  # обычно 2–4; до 8 — когда выбор из списка (например, что проверить из 5 утверждений)
TRUE = {"1", "true", "yes", "да", "y", "д"}

PRE_ANSWER: List[Callable[[Dict[str, Any], str], Optional[Dict[str, Any]]]] = []
POST_ANSWER: List[Callable[[Dict[str, Any]], Any]] = []


HUMAN_PROMPTS = "human_prompts.jsonl"  # пишет хук UserPromptSubmit (prompt_context.py): {"ts","text","session_id"}
_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', "ё": "е",
                         "—": "-", "–": "-", "−": "-"})


def _norm(text: Any) -> str:
    t = " ".join(str(text or "").lower().translate(_QUOTES).split())
    return t.strip(" .!;:")


PENDING_PROMPTS = ".pending_prompts.jsonl"  # projects/: реплики, пришедшие до `bulochka new` (в .gitignore)
PENDING_KEEP = 20


def pending_prompts_path():
    return core.projects_dir() / PENDING_PROMPTS


def stash_prompt(record: Dict[str, Any]) -> None:
    """Реплика человека, пока проекта нет: первая реплика создаёт проект внутри того же вызова, а хук
    UserPromptSubmit срабатывает раньше. Перенос в журнал реплик — при `bulochka new` (adopt_pending_prompts)."""
    core.append_jsonl(pending_prompts_path(), record)


def adopt_pending_prompts(slug: str) -> int:
    """Перенести отложенные реплики в state/human_prompts.jsonl нового проекта; вернуть их число."""
    p = pending_prompts_path()
    try:
        rows = core.read_jsonl(p)
    except (OSError, ValueError):
        rows = []
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("text") or "").strip()][-PENDING_KEEP:]
    for r in rows:
        core.append_jsonl(core.state_dir(slug) / HUMAN_PROMPTS, dict(r, pending=True))
    try:
        p.unlink()
    except OSError:
        pass
    return len(rows)


def human_prompts(slug: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Последние сообщения человека в чате (их пишет хук; без хука — пусто)."""
    try:
        return core.read_jsonl(core.state_dir(slug) / HUMAN_PROMPTS)[-limit:]
    except (OSError, ValueError, core.BulochkaError):
        return []


def verbatim_problem(g: Dict[str, Any], text: str, raw: str, slug: Optional[str]) -> Optional[str]:
    """Ответ на ворота — дословный фрагмент сообщения человека: из последнего сообщения или из любого после
    вопроса. Без журнала сообщений (хуки не работают) и из терминала человека — не сверяется."""
    from .journal import is_human
    if is_human():
        return None
    prompts = human_prompts(slug)
    if not prompts:
        return None
    since = g.get("created_at") or ""
    cands = [p for p in prompts if (p.get("ts") or "") >= since] + [prompts[-1]]
    for variant in {_norm(text), _norm(raw)}:
        if variant and any(variant in _norm(p.get("text")) for p in cands):
            return None
    return f"в сообщениях человека после вопроса нет «{raw[:120]}»"


class AnswerRefused(core.BulochkaError):
    """Ответ не записан: отказ валидатора PRE_ANSWER (refusal — запись отказа или словарь tag/phrase/next_action)."""

    def __init__(self, refusal: Dict[str, Any]):
        self.refusal = refusal
        super().__init__(str(refusal.get("phrase") or refusal.get("reason") or "ответ не принят"))


def run_pre_answer(g: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Первый отказ PRE_ANSWER или None."""
    for fn in ext.active(PRE_ANSWER):
        try:
            res = fn(g, text)
        except Exception as e:  # noqa: BLE001
            ext.warn(f"PRE_ANSWER {getattr(fn, '__name__', fn)} упал: {type(e).__name__}: {e}")
            continue
        if res:
            if not isinstance(res, dict):
                res = {"tag": "HUMAN_DECISION", "target_id": g.get("id"), "reason": "pre_answer", "phrase": str(res)}
            return res
    return None


def run_post_answer(g: Dict[str, Any]) -> None:
    for fn in ext.active(POST_ANSWER):
        try:
            fn(g)
        except Exception as e:  # noqa: BLE001
            ext.warn(f"POST_ANSWER {getattr(fn, '__name__', fn)} упал: {type(e).__name__}: {e}")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in TRUE


def _json_or_text(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _split_ids(raw: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for chunk in raw or []:
        out += [x.strip() for x in str(chunk).split(",") if x.strip()]
    return out


def ask(kind: str, question: str, options: List[str], *, why: str = "", default: str = "", flips: bool = False,
        claims: Optional[List[str]] = None, outcomes: Any = None, cost_if_wrong: Any = None, blind: bool = False,
        filtered: int = 0, step: Optional[str] = None, slug: Optional[str] = None):
    """Возвращает (gate, created: bool, warnings)."""
    from . import project
    slug = slug or core.current_slug()
    core.project_dir(slug)
    if kind not in KINDS:
        raise core.BulochkaError(f"--kind: одно из {', '.join(KINDS)}")
    if not question.strip():
        raise core.BulochkaError("--question пустой.")
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise core.BulochkaError(f"--options: от {MIN_OPTIONS} до {MAX_OPTIONS} вариантов ответа, сейчас {len(options)}.")
    if kind in ("choose_option", "approve_decision") and not str(default).strip():
        default = "не выбирать: выбор варианта и утверждение решения по умолчанию не делаются"
    gates = core.latest("gate", slug)
    # Повтор того же ворот человека (не question), пока не отвечен, — возвращаем существующий
    if kind != "question":
        for g in gates.values():
            if g.get("kind") == kind and g.get("answer") in (None, ""):
                return g, False, []
    if step is None:
        try:
            step = project.compute(slug).get("step") or "revise"
        except core.BulochkaError:
            step = "intake"
    claims = claims or []
    warnings: List[Dict[str, Any]] = []
    if len(claims) > MAX_CLAIMS_SHOWN:
        warnings.append({"rule": "W08", "level": "warning", "ids": claims,
                         "msg": f"показано {len(claims)} утверждений, больше {MAX_CLAIMS_SHOWN}"})
    if not why.strip():
        warnings.append({"rule": "W08", "level": "warning", "ids": [], "msg": "нет --why: зачем спрашиваем"})
    if flips and outcomes is None:
        warnings.append({"rule": "W08", "level": "warning", "ids": [],
                         "msg": "--flips без --outcomes: не видно, как ответ меняет рекомендацию"})
    if kind == "question" and step == "intake":
        n = sum(1 for g in gates.values() if g.get("kind") == "question" and g.get("step") == "intake")
        if n >= MAX_INTAKE_QUESTIONS:
            warnings.append({"rule": "INTAKE_Q", "level": "warning", "ids": [],
                             "msg": f"на постановке уже {n} вопроса — больше {MAX_INTAKE_QUESTIONS} не задаём"})
    rec = {
        "id": core.next_id("gate", slug), "step": step, "kind": kind, "question": question.strip(),
        "options": options, "claims_shown": claims, "flips": bool(flips), "why_ask": why,
        "outcomes_preview": outcomes, "cost_if_wrong": cost_if_wrong if cost_if_wrong is not None else {},
        "default_if_no_answer": default, "blind_choice": bool(blind), "answer": None, "answered_by": None,
        "answered_at": None, "review_seconds": None, "filtered_questions": int(filtered),
        "shown_at": core.now_iso(), "actor": "agent",
    }
    core.put("gate", rec, slug)
    _refresh(slug)
    return rec, True, warnings


_NUM_ANSWER = re.compile(r"^\s*(?:№\s*)?(\d{1,2})\s*(?:$|[.)]\s*|\s*[—–:-]\s*|\s+(?=\S))(.*)$", re.S)


def option_by_number(text: str, options: List[str]) -> Tuple[str, str]:
    """«1», «1.», «1)», «1 — комментарий», «1. Данных нет» → (текст варианта 1, комментарий).
    Номер вне списка вариантов или число как ответ («2 семьи») — ответ как есть."""
    m = _NUM_ANSWER.match(text or "")
    if not m or not options:
        return text, ""
    if re.match(r"^\s*(?:№\s*)?\d+[.,]\d", text):  # «1.5», «2,5» — дробь, а не номер
        return text, ""
    n, note = int(m.group(1)), m.group(2).strip()
    if not 1 <= n <= len(options):
        return text, ""
    sep = text[m.start(1) + len(m.group(1)):m.start(2)] if note else ""
    if note and not re.search(r"[.)—–:-]", sep):  # «2 семьи», «3 недели» — это число, а не номер варианта
        return text, ""
    return options[n - 1], note


def answer(gate_id: str, text: str, *, seconds: Optional[int] = None, slug: Optional[str] = None) -> Dict[str, Any]:
    from . import journal
    slug = slug or core.current_slug()
    g = core.get("gate", gate_id, slug)
    if g is None:
        raise core.BulochkaError(f"Ворот {gate_id} нет.")
    text = (text or "").strip()
    if not text:
        raise core.BulochkaError("--answer пустой.")
    raw = text
    text, note = option_by_number(text, g.get("options") or [])
    if g.get("answer") not in (None, ""):
        if g["answer"] == text:
            return g
        raise core.BulochkaError(f"{gate_id} уже отвечен: «{g['answer']}». Новый ответ — новые ворота (bulochka ask).")
    problem = verbatim_problem(g, text, raw, slug)
    if problem:
        from . import refusals
        cost = refusals.DEFAULT_COST["HUMAN_DECISION"]
        phrase = refusals.phrase("", f"Ответ на {gate_id} не записываю", "HUMAN_DECISION", problem,
                                 "Меняется: ворота остаются без ответа",
                                 "запиши его слова дословно — фрагментом сообщения, без пересказа и дополнений; "
                                 "человек ещё не ответил — дождись ответа", cost)
        raise AnswerRefused(refusals.record(
            "HUMAN_DECISION", target_id=gate_id, reason="answer_not_verbatim", phrase_text=phrase,
            next_action=f"bulochka answer {gate_id} --answer \"<дословно из сообщения человека>\"", cost=cost,
            slug=slug))
    refusal = run_pre_answer(dict(g), text)
    if refusal:
        raise AnswerRefused(refusal)
    now = core.now_iso()
    if seconds is None:
        t0 = journal.parse_ts(g.get("shown_at") or g.get("created_at"))
        t1 = journal.parse_ts(now)
        seconds = int((t1 - t0).total_seconds()) if t0 and t1 else None
    rec = dict(g)
    rec.update({"answer": text, "answered_by": "human", "answered_at": now, "review_seconds": seconds})
    if raw != text:  # ответ номером: вариант — в answer, дословные слова человека — рядом
        rec.update({"answer_raw": raw, "answer_note": note or None})
    core.put("gate", rec, slug)
    journal.event("gate.answer", step=g.get("step"), actor="human", slug=slug, review_seconds=seconds,
                  gate_id=gate_id)
    _refresh(slug)
    run_post_answer(dict(rec))
    return rec


def _refresh(slug: Optional[str]) -> None:
    try:
        from . import project
        project.refresh_state(slug)
    except Exception:  # noqa: BLE001
        pass


def cmd_ask(args) -> int:
    rec, created, warnings = ask(
        args.kind, args.question, args.options or [], why=args.why or "", default=args.default or "",
        flips=bool(args.flips), claims=_split_ids(args.claims),
        outcomes=_json_or_text(args.outcomes), cost_if_wrong=_json_or_text(args.cost_if_wrong),
        blind=args.blind, filtered=args.filtered, step=args.step)
    human = (f"{rec['id']}: {rec['question']}\n" + "\n".join(f"  {i}. {o}" for i, o in enumerate(rec["options"], 1))
             + ("" if created else "\n(ворота уже заданы и ждут ответа)"))
    return core.emit("ask", {"gate_id": rec["id"], "created": created}, errors=warnings, as_json=args.json,
                     human=human, next_cmd=f"bulochka answer {rec['id']} --answer «…» (после ответа человека)")


def cmd_answer(args) -> int:
    try:
        rec = answer(args.gate_id, args.answer, seconds=args.seconds)
    except AnswerRefused as e:
        from . import refusals
        payload = refusals.as_payload(e.refusal)
        return core.emit("answer", {"gate_id": args.gate_id}, refusal=payload, next_cmd=payload.get("next_action"),
                         as_json=args.json, code=core.EXIT_REFUSAL)
    return core.emit("answer", {"gate_id": rec["id"], "review_seconds": rec.get("review_seconds"),
                                "answer": rec.get("answer"), "answer_raw": rec.get("answer_raw"),
                                "answer_note": rec.get("answer_note")}, as_json=args.json,
                     human=f"{rec['id']}: ответ записан — «{rec['answer']}»"
                           + (f" (человек ответил «{rec['answer_raw']}»)" if rec.get("answer_raw") else "")
                           + f" ({rec.get('review_seconds')} с).")


def register(sub) -> None:
    p = sub.add_parser("ask", help="зарегистрировать вопрос человеку (ворота)")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--question", required=True)
    p.add_argument("--options", nargs="+", required=True, help="2–8 вариантов ответа (обычно 2–4)")
    p.add_argument("--flips", action="store_true", help="ответ меняет рекомендацию")
    p.add_argument("--why", help="зачем спрашиваем")
    p.add_argument("--default", help="допущение, если ответа нет")
    p.add_argument("--claims", nargs="*", help="показанные C### (≤7)")
    p.add_argument("--outcomes", help='JSON {"ответ": "рекомендация"}')
    p.add_argument("--cost-if-wrong", dest="cost_if_wrong", help='JSON {"rub_equiv_range":[…],"note":"…"}')
    p.add_argument("--blind", action="store_true", help="рекомендация скрыта до ответа")
    p.add_argument("--filtered", type=int, default=0, help="сколько вопросов не задано")
    p.add_argument("--step", help="шаг (по умолчанию текущий)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("answer", help="записать ответ человека")
    p.add_argument("gate_id")
    p.add_argument("--answer", required=True)
    p.add_argument("--seconds", type=int, help="время на ответ, если измерено иначе")
    p.set_defaults(func=cmd_answer)
