"""Восемь отказов (контракт §3): теги, шаблоны фраз, запись в refusals.jsonl.

Шаблон фразы: что сделал + чего не делаю (ТЕГ) + что это меняет + следующее действие с ценой.
Без «извините».
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import core

TAGS = (
    "NO_SOURCE",
    "NO_PRODUCT_PROPERTY",
    "SYNTHETIC_AS_FACT",
    "A_CONFLICT",
    "HUMAN_DECISION",
    "INCOMPARABLE_METRICS",
    "STALE_SOURCE",
    "BUDGET",
)

# Цена следующего действия по умолчанию: токены агента и минуты продакта
DEFAULT_COST = {
    "NO_SOURCE": {"tokens": 8000, "minutes_human": 1},
    "NO_PRODUCT_PROPERTY": {"tokens": 3000, "minutes_human": 2},
    "SYNTHETIC_AS_FACT": {"tokens": 3000, "minutes_human": 240},
    "A_CONFLICT": {"tokens": 5000, "minutes_human": 2},
    "HUMAN_DECISION": {"tokens": 0, "minutes_human": 2},
    "INCOMPARABLE_METRICS": {"tokens": 4000, "minutes_human": 0},
    "STALE_SOURCE": {"tokens": 5000, "minutes_human": 0},
    "BUDGET": {"tokens": 0, "minutes_human": 1},
}


def fmt_cost(cost: Dict[str, Any]) -> str:
    parts = []
    t = int(cost.get("tokens") or 0)
    m = cost.get("minutes_human") or 0
    if t:
        parts.append(f"~{t // 1000} тыс. токенов" if t >= 1000 else f"~{t} токенов")
    if m:
        if m >= 60:
            parts.append(f"~{round(m / 60, 1):g} ч вашего времени")
        else:
            parts.append(f"{m:g} мин вашего времени")
    return ", ".join(parts) if parts else "без затрат"


def phrase(done: str, not_doing: str, tag: str, detail: str, changes: str, next_action: str,
           cost: Dict[str, Any]) -> str:
    """Собрать фразу по шаблону. Части — короткие предложения без точки в конце.

    «Записал C014 как гипотезу. Как факт не беру (NO_SOURCE): цитаты нет на странице S014.
    Из-за этого O002 держится на допущении. Дальше: найти источник (~8 тыс. токенов)»
    """
    head = f"{not_doing} ({tag})" + (f": {detail}" if detail else "")
    parts = [p for p in (done, head, changes) if p]
    return ". ".join(parts) + f". Дальше: {next_action} ({fmt_cost(cost)})."


# Шаги workflow (совпадают с project.STEPS): intake, plan, research, situation, options, strategy,
# validation-plan, review, decide, revise
STEP_IDS = ("intake", "plan", "research", "situation", "options", "strategy", "validation-plan", "review",
            "decide", "revise")


def current_step(slug: Optional[str] = None) -> str:
    """Текущий шаг из project.compute (срез состояния); все шаги пройдены → revise; сбой → unknown."""
    try:
        from . import project  # type: ignore
        return str(project.compute(slug).get("step") or "revise")
    except Exception:
        return "unknown"


def record(tag: str, *, target_id: str, reason: str, phrase_text: str, next_action: str,
           cost: Optional[Dict[str, Any]] = None, actor: str = "agent", step: Optional[str] = None,
           outcome: str = "pending", extra: Optional[Dict[str, Any]] = None, slug: Optional[str] = None) -> Dict[str, Any]:
    if tag not in TAGS:
        raise ValueError(f"неизвестный тег отказа {tag}")
    rec: Dict[str, Any] = {
        "tag": tag,
        "rule_id": f"RF-{tag}-1",
        "target_id": target_id,
        "step": step or current_step(slug),
        "reason": reason,
        "phrase": phrase_text,
        "next_action": next_action,
        "next_action_cost": cost or DEFAULT_COST[tag],
        "outcome": outcome,
        "override_reason": None,
        "actor": actor,
        "ts": core.now_iso(),
    }
    if extra:
        rec.update(extra)
    return core.put("refusal", rec, slug)


def as_payload(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Сокращённый отказ для поля `refusal` в JSON-ответе CLI."""
    return {k: rec.get(k) for k in ("id", "tag", "target_id", "reason", "phrase", "next_action", "next_action_cost")}
