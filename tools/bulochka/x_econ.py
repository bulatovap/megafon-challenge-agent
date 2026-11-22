"""Цена решения — общие помощники фич: история прогонов, связь «токены ↔ % недели», рубли из подписки.

Деньги — доля стоимости подписки (оценка), а не цена API: ₽ = цена за неделю × Δ% недельного лимита × курс.
История прогонов — `knowledge/runs/history.jsonl` (одна строка на прогон, ключ run_id).
Модуль ничего не регистрирует в CLI: его импортируют x_smeta, x_flip, x_budget, x_pricetag.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core, cost

STEP_IDS = ["intake", "plan", "research", "situation", "options", "strategy", "validation-plan", "review",
            "decide", "revise"]
STEP_BY_N = {1: "intake", 2: "plan", 3: "research", 4: "situation", 5: "options", 6: "strategy",
             7: "validation-plan", 8: "review", 9: "review", 10: "decide", 11: "revise"}
SEARCH_STEP = "research"
CHECK_STEP = "review"
DEFAULT_CALC_TOKENS = 20000  # оценка шага расчёта, пока в истории нет своей
DEFAULT_QUESTIONS = 6
SUB_NOTE = "из стоимости подписки"


# ---------- история прогонов ----------

def history_path() -> Path:
    return core.root() / "knowledge" / "runs" / "history.jsonl"


def history(exclude_run: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = [r for r in core.read_jsonl(history_path()) if isinstance(r, dict) and r.get("run_id")]
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:  # последняя строка с тем же run_id
        seen[r["run_id"]] = r
    return [r for k, r in seen.items() if k != exclude_run]


def append_history(row: Dict[str, Any]) -> bool:
    """Дописать прогон; False — такой run_id уже есть (повтор не дублирует)."""
    if any(r.get("run_id") == row.get("run_id") for r in history()):
        return False
    core.append_jsonl(history_path(), row)
    return True


def median(xs: List[float]) -> Optional[float]:
    xs = [float(x) for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def quantile(xs: List[float], q: float) -> Optional[float]:
    xs = sorted(float(x) for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# ---------- токены ↔ % недели ↔ рубли ----------

def week_ratio(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """% недельного лимита на 1 токен: по замерам (weekly_delta) или, если их нет, по диапазону человека.

    {"kind": "measured", "per_token": x} | {"kind": "manual", "per_token_range": [a, b], "note": …} | {"kind": None}
    """
    rows = history() if rows is None else rows
    measured = [r["weekly_delta"] / r["tokens"] for r in rows
                if isinstance(r.get("weekly_delta"), (int, float)) and r["weekly_delta"] > 0 and r.get("tokens")]
    if measured:
        return {"kind": "measured", "per_token": median(measured), "runs": len(measured)}
    manual = [(r["weekly_delta_range"], r["tokens"], r.get("weekly_delta_source") or "замер человека") for r in rows
              if isinstance(r.get("weekly_delta_range"), list) and len(r["weekly_delta_range"]) == 2 and r.get("tokens")]
    if manual:
        lo = median([m[0][0] / m[1] for m in manual])
        hi = median([m[0][1] / m[1] for m in manual])
        return {"kind": "manual", "per_token_range": [lo, hi], "note": manual[0][2], "runs": len(manual)}
    return {"kind": None}


def pct_for_tokens(tokens: Optional[float], ratio: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Доля недели и ₽ из подписки для объёма токенов: точка, диапазон или «замеров нет»."""
    ratio = week_ratio() if ratio is None else ratio
    if not tokens or not ratio.get("kind"):
        return {"pct": None, "pct_range": None, "rub": None, "rub_range": None, "basis": None}
    if ratio["kind"] == "measured":
        pct = round(tokens * ratio["per_token"], 2)
        return {"pct": pct, "pct_range": None, "rub": cost.money_from_pct(pct)[1], "rub_range": None,
                "basis": f"замеры недельного лимита, прогонов: {ratio['runs']}"}
    a, b = (round(tokens * x, 2) for x in ratio["per_token_range"])
    return {"pct": None, "pct_range": [a, b], "rub": None,
            "rub_range": [cost.money_from_pct(a)[1], cost.money_from_pct(b)[1]],
            "basis": ratio.get("note") or "замер человека"}


def fmt_pct_money(pm: Dict[str, Any]) -> Tuple[str, str]:
    """(строка % недели, строка ₽) для вывода; без замеров — честное «замеров нет»."""
    if pm.get("pct") is not None:
        return f"{pm['pct']:g}% недельного лимита", f"≈{pm['rub']:.0f} ₽ {SUB_NOTE}"
    if pm.get("pct_range"):
        a, b = pm["pct_range"]
        ra, rb = pm["rub_range"]
        return (f"{a:g}–{b:g}% недельного лимита ({pm['basis']})", f"≈{ra:.0f}–{rb:.0f} ₽ {SUB_NOTE}")
    return "недельный лимит: замеров нет", "₽ из стоимости подписки: замеров нет — без доли недели рубли не считаю"


# ---------- прочее ----------

def steps_of_brief(text: str) -> List[str]:
    """Шаги из строки «Шаги: …» брифа: «все 11», «1–7 (+10)», «1–7, 10», «11 (`/reforecast`), затем 5–7 и 10»."""
    m = re.search(r"Шаги:\s*([^\n·]+)", text or "")
    if not m:
        return [s for s in STEP_IDS if s != "revise"]
    spec = m.group(1)
    if re.search(r"\bвсе\b", spec):
        return list(STEP_IDS)
    nums: List[int] = []
    for a, b in re.findall(r"(\d+)\s*[–—-]\s*(\d+)", spec):
        nums += list(range(int(a), int(b) + 1))
    spec2 = re.sub(r"\d+\s*[–—-]\s*\d+", " ", spec)
    nums += [int(x) for x in re.findall(r"\d+", spec2)]
    out = [s for s in STEP_IDS if any(STEP_BY_N.get(n) == s for n in nums)]
    return out or [s for s in STEP_IDS if s != "revise"]


def brief_id(text: str, path: Optional[str] = None) -> Optional[str]:
    m = re.search(r"\bB0\d\b", (text or "")[:200]) or (re.search(r"B0\d", path or "") if path else None)
    return m.group(0) if m else None


def plan_questions(slug: Optional[str] = None) -> Optional[int]:
    """Число вопросов плана (Q1…Qn в cost_plan.md), None — плана нет."""
    try:
        p = core.project_dir(slug) / "cost_plan.md"
    except core.BulochkaError:
        return None
    if not p.exists():
        return None
    qs = set(re.findall(r"\bQ(\d{1,2})\b", p.read_text(encoding="utf-8")))
    return len(qs) or None


def step_tokens(cost_data: Dict[str, Any]) -> Dict[str, int]:
    return {k: cost.headline(v) for k, v in (cost_data.get("tokens_by_step") or {}).items()
            if isinstance(v, dict)}


def calc_tokens_estimate(rows: Optional[List[Dict[str, Any]]] = None) -> int:
    rows = history() if rows is None else rows
    m = median([r.get("calc_tokens") for r in rows])
    return int(m) if m else DEFAULT_CALC_TOKENS


def verify_claim_tokens(rows: Optional[List[Dict[str, Any]]] = None) -> int:
    """Оценка токенов на проверку одного утверждения: из истории, иначе цена следующего действия отказа NO_SOURCE (refusals.DEFAULT_COST)."""
    rows = history() if rows is None else rows
    m = median([r.get("verify_claim_tokens") for r in rows])
    if m:
        return int(m)
    from . import refusals
    return int(refusals.DEFAULT_COST["NO_SOURCE"]["tokens"])


def save_cost(slug: Optional[str], data: Dict[str, Any]) -> None:
    core.atomic_write_json(cost.cost_path(slug), data)


def set_step_budget(step: str, tokens: int, slug: Optional[str] = None) -> None:
    """То же, что `bulochka cost --step <шаг> --set-budget N`."""
    prev = cost.load_cost(slug)
    prev.setdefault("step_budgets", {})[step] = int(tokens)
    save_cost(slug, prev)


def deferred_path(slug: Optional[str] = None) -> Path:
    return core.state_dir(slug) / "deferred.json"


def load_deferred(slug: Optional[str] = None) -> Dict[str, Any]:
    try:
        d = json.loads(deferred_path(slug).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_deferred(d: Dict[str, Any], slug: Optional[str] = None) -> None:
    core.atomic_write_json(deferred_path(slug), d)
