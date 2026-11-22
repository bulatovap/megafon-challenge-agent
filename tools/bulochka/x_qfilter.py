"""Вопрос-переворот с видимой развилкой: `bulochka qfilter --candidates <JSON|файл>`.

Каждый кандидат в вопросы продакту проходит тест на разворот: задаём только те, от ответа на которые может
смениться рекомендация, и показываем обе ветки до ответа. Остальные идут в счётчик `filtered_questions`
(«отфильтровано N — не меняли вывод»). Кандидат: {"question", "switch", "plausible", "if_above", "if_below",
"options": [...], "default": "..."}; без switch — вопрос не может сменить выбор и отфильтровывается.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core, cost, gates, journal, x_flip

MAX_ASK = 3


def flips(c: Dict[str, Any]) -> Dict[str, Any]:
    """Может ли ответ на вопрос сменить выбор: порог внутри правдоподобного диапазона."""
    if not c.get("switch") or not c.get("plausible"):
        return {"flips": False, "why": "порог переворота не назван"}
    sw = x_flip.parse_switch(c["switch"])
    lo_n, hi_n = x_flip.parse_range(c["plausible"])
    t = float(sw["value"])
    lo, hi = x_flip._same_scale(sw, lo_n), x_flip._same_scale(sw, hi_n)
    unit = sw["unit"] if sw["unit"] != "count" else hi_n["unit"]
    ge = c.get("direction", "ge") == "ge"
    crosses = (lo < t <= hi) if ge else (lo <= t < hi)
    sign = "≥" if ge else "≤"
    rng = x_flip.show_range(lo, hi, unit)
    return {"flips": crosses, "threshold": f"{sign}{x_flip.show(t, unit)}", "plausible": rng,
            "why": (f"порог {sign}{x_flip.show(t, unit)} внутри правдоподобного {rng}" if crosses
                    else f"порог {sign}{x_flip.show(t, unit)} вне правдоподобного {rng}")}


def load_candidates(raw: str) -> List[Dict[str, Any]]:
    text = raw
    if not raw.lstrip().startswith(("[", "{")):
        p = Path(raw)
        if not p.exists():
            raise core.BulochkaError(f"--candidates: файл {raw} не найден.")
        text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        raise core.BulochkaError("--candidates: JSON-список кандидатов или путь к такому файлу.")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(x, dict) and x.get("question") for x in data):
        raise core.BulochkaError("--candidates: у каждого кандидата нужен question.")
    return data


def run(cands: List[Dict[str, Any]], ask: bool = True, slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    kept, dropped = [], []
    for c in cands:
        f = flips(c)
        (kept if f["flips"] else dropped).append(dict(c, **{"test": f}))
    overflow = kept[MAX_ASK:]
    kept = kept[:MAX_ASK]
    filtered = len(dropped)
    asked = []
    if ask:
        for i, c in enumerate(kept):
            opts = c.get("options") or [f"{c['test']['threshold']} и больше", "меньше"]
            outcomes = {opts[0]: c.get("if_above") or "рекомендация меняется",
                        opts[-1]: c.get("if_below") or "рекомендация остаётся"}
            g, _, _ = gates.ask("question", c["question"], list(opts), why=c["test"]["why"],
                                default=c.get("default") or opts[-1], flips=True, outcomes=outcomes,
                                filtered=filtered if i == 0 else 0, slug=slug)
            asked.append(g["id"])
    journal.event("questions.filtered", step=cost.current_step(slug), actor="agent", slug=slug,
                  asked=len(kept), filtered=filtered, deferred_over_limit=len(overflow))
    phrase = (f"Спрашиваю {len(kept)} из {len(cands)}: только то, от чего меняется рекомендация. "
              f"Отфильтровано {filtered} — не меняли вывод"
              + (f" ({'; '.join(d['question'] + ': ' + d['test']['why'] for d in dropped[:3])})" if dropped else "")
              + "." + (f" Ещё {len(overflow)} переворачивающих — в следующий заход (не больше {MAX_ASK} за раз)."
                       if overflow else ""))
    return {"asked": [c["question"] for c in kept], "gate_ids": asked, "filtered": filtered,
            "filtered_questions": [d["question"] for d in dropped], "overflow": [c["question"] for c in overflow],
            "phrase": phrase,
            "outcomes": {c["question"]: {"если " + c["test"]["threshold"]: c.get("if_above") or "рекомендация меняется",
                                         "иначе": c.get("if_below") or "рекомендация остаётся"} for c in kept}}


def cmd_qfilter(args) -> int:
    r = run(load_candidates(args.candidates), ask=not args.dry_run)
    lines = [r["phrase"]]
    for q, br in r["outcomes"].items():
        lines.append(f"- {q}: " + "; ".join(f"{k} → {v}" for k, v in br.items()))
    return core.emit("qfilter", r, human="\n".join(lines), as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("qfilter", help="задать только вопросы, ответ на которые может сменить рекомендацию")
    p.add_argument("--candidates", required=True, help="JSON-список кандидатов или путь к файлу")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="только отфильтровать, ворота не создавать")
    p.set_defaults(func=cmd_qfilter)
