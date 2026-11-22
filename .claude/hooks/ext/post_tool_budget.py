#!/usr/bin/env python3
"""PostToolUse: бюджет шага посреди автономного шага (без реплик человека).

1. Если продакт уже выбрал одно утверждение (`budget resolve` → grant в state/deferred.json) — молчим,
   пока оно не проверено и бюджет не исчерпан; затем один раз: «проверено — останавливаюсь».
2. Иначе — `budget.check(stdin)` хука бюджета: отказ BUDGET пишется один раз на шаг и бюджет; фраза уходит
   в additionalContext, пока по отказу не заданы ворота (`bulochka budget ask`).
Сбой → incidents.jsonl, exit 0.
"""
from __future__ import annotations

import sys

import _econ_hook as h

EVENT = "PostToolUse"
NEXT = (" Дальше: `python3 -m tools.bulochka budget ask` — покажи продакту варианты, дождись ответа, "
        "`answer G### --answer «…»` дословно, затем `budget resolve G###`.")


def is_verified(c):
    if not c:
        return False
    try:
        import budget  # хук базового слоя: та же мера «проверено», что в списке BUDGET
        return budget.is_checked(c)
    except ImportError:
        return c.get("verified") is True or c.get("number_on_page") == "pass"


def handle(stdin):
    if stdin.get("hook_event_name") not in (None, EVENT):
        return None
    slug = h.slug()
    if not slug:
        return None
    from tools.bulochka import core, cost, x_budget
    step = cost.current_step(slug)
    grant = x_budget.active_grant(step, slug)
    if grant:
        d = cost.compute(slug, stdin=stdin, writer="budget")
        spent = cost.headline(d.get("tokens_step") or {})
        budget = int(d.get("step_budget_tokens") or 0)
        if budget == int(grant.get("budget") or 0):
            claim = core.get("claim", grant["claim"], slug)
            if is_verified(claim):
                why = f"{grant['claim']} проверено"
            elif spent >= budget:
                why = f"бюджет на проверку {grant['claim']} исчерпан ({cost.fmt_tok(spent)} из {cost.fmt_tok(budget)} ток)"
            else:
                return None
            x_budget.close_grant(grant, why, slug)
            return (f"{why[0].upper() + why[1:]}. По решению продакта ({grant['gate_id']}) шаг останавливается: "
                    "`python3 -m tools.bulochka receipt`, затем `python3 -m tools.bulochka pause`. "
                    "Отложенные утверждения не проверяй.")
        x_budget.close_grant(grant, "бюджет шага изменён вручную", slug)
    import budget  # хук бюджета: check() без побочного вывода
    msg = budget.check(stdin)
    if not msg:
        return None
    ref = x_budget.pending_budget_refusal(slug)
    if ref and ref.get("gate_id"):
        return None  # ворота уже заданы — ждём продакта, не шумим
    return msg + NEXT


def main():
    try:
        stdin = h.read_stdin()
        msg = handle(stdin)
        if msg:
            h.context(EVENT, msg)
    except Exception as e:  # noqa: BLE001
        h.incident("post_tool_budget", f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
