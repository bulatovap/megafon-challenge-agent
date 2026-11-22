"""UserPromptSubmit: бюджет шага в токенах. При > 80% step_budget_tokens — отказ BUDGET.

Отказ пишется в refusals.jsonl один раз на шаг и бюджет; сообщение уходит агенту в additionalContext,
пока шаг выше порога. Не блокирует ввод (exit 0). Внутренняя ошибка → incidents.jsonl, exit 0.

Шаг с активным выбором продакта пропускается: в state/deferred.json есть незакрытый grant этого шага
({"grants": [{"step", "claim", "budget", "gate_id", "refusal_id", "closed": false}, …]}) с тем же бюджетом —
продакт уже решил, что проверить, повторный BUDGET не заводится. Бюджет изменён — grant больше не действует.

check(stdin) -> str | None — импортируемая функция без побочного вывода (её зовут и ext-хуки).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def is_checked(c):
    """Уже проверено: отмечено verified, число найдено на странице или факт с цитатой, совпавшей с источником
    (живой прогон 21.11: BUDGET предлагал «проверить» факты, цитаты которых уже сверены)."""
    if c.get("verified") is True or c.get("number_on_page") == "pass":
        return True
    return (c.get("type") == "fact" and bool(c.get("quote_offset"))
            and c.get("number_on_page") not in ("fail", "needs_manual"))


def unverified_material(core, slug, limit=5):
    """Непроверенные material-утверждения: сначала те, на которых стоят решения и варианты."""
    out = []
    for c in core.latest("claim", slug).values():
        if c.get("status", "active") != "active" or not c.get("material"):
            continue
        if is_checked(c):
            continue
        out.append(c)
    out.sort(key=lambda c: (not c.get("decision_link"), c["id"]))
    return [c["id"] for c in out][:limit]


def active_grant(core, slug, step, budget):
    """Незакрытый выбор продакта для шага из state/deferred.json (тот же бюджет) или None."""
    p = core.state_dir(slug) / "deferred.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    for gr in reversed(d.get("grants") or [] if isinstance(d, dict) else []):
        if not isinstance(gr, dict) or gr.get("closed") or gr.get("step") != step:
            continue
        if gr.get("budget") in (None, "") or int(gr.get("budget") or 0) == int(budget or 0):
            return gr
    return None


def check(stdin):
    """Вернуть текст для additionalContext или None."""
    from tools.bulochka import core, cost
    slug = core.current_slug()
    if not slug or not (core.projects_dir() / slug).exists():
        return None
    d = cost.compute(slug, stdin=stdin, writer="budget")
    cfg = cost.pricing()
    threshold = float(cfg.get("budget_threshold") or 0.8)
    step = d.get("step") or cost.NO_STEP
    spent = cost.headline(d.get("tokens_step") or {})
    budget = int(d.get("step_budget_tokens") or 0)
    if not budget or spent <= threshold * budget:
        return None
    if active_grant(core, slug, step, budget):
        return None
    pct = round(spent * 100 / budget)
    ids = unverified_material(core, slug)
    ids_txt = ", ".join(ids) if ids else "—"
    phrase = (f"Потрачено {pct}% бюджета шага «{step}»: {cost.fmt_tok(spent)} из {cost.fmt_tok(budget)} токенов (BUDGET). "
              + (f"Не успею проверить {ids_txt}. " if ids else "")
              + "Выберите, что проверить в первую очередь, или поднимите бюджет: "
              + "`python3 -m tools.bulochka cost --step --set-budget <токены>`.")
    existing = [x for x in core.latest("refusal", slug).values()
                if x.get("tag") == "BUDGET" and x.get("step") == step and x.get("budget") == budget]
    if not existing:
        core.put("refusal", {
            "tag": "BUDGET", "rule_id": "RF-BUDGET-1", "target_id": step, "step": step,
            "reason": f"step_tokens {spent} > {threshold:g} × step_budget_tokens {budget}",
            "phrase": phrase, "next_action": "выбор продакта или bulochka cost --set-budget",
            "next_action_cost": {"tokens": 0, "minutes_human": 1},
            "outcome": "pending", "actor": "hook", "ts": core.now_iso(),
            "spent": spent, "budget": budget, "unverified_ids": ids,
        }, slug)
    return phrase


def incident(err):
    try:
        from tools.bulochka import core
        slug = core.current_slug()
        if slug and (core.projects_dir() / slug).exists():
            core.append_jsonl(core.state_dir(slug) / "incidents.jsonl",
                              {"ts": core.now_iso(), "hook": "budget", "error": err})
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        stdin = json.loads(raw) if raw.strip() else {}
        if not isinstance(stdin, dict):
            stdin = {}
        msg = check(stdin)
        if msg:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": msg}}, ensure_ascii=False))
    except Exception as e:
        incident(f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
