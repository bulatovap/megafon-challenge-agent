"""Бюджетный предохранитель с выбором продакта: `bulochka budget ask | resolve`.

Отказ BUDGET пишет хук бюджета (на реплике человека и после каждого инструмента — ext-хук post_tool_budget).
`budget ask` превращает последний ждущий отказ в ворота: проверить одно из непроверенных утверждений,
поднять бюджет или остановиться. `budget resolve G###` исполняет ответ продакта:
- C### — бюджет шага растёт ровно на оценку одной проверки, остальные ID уходят в state/deferred.json, отказ accepted;
- «поднять бюджет до N» — бюджет N, отказ overridden с причиной;
- «остановиться» — pause + квитанция остановки, отказ accepted.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from . import core, cost, gates, journal, x_econ

STOP_LABEL = "остановиться"
CLAIM_RE = re.compile(r"\bC\d{3,}\b")


def pending_budget_refusal(slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    items = [x for x in core.latest("refusal", slug).values()
             if x.get("tag") == "BUDGET" and x.get("outcome", "pending") == "pending"]
    return sorted(items, key=lambda x: x["id"])[-1] if items else None


def raise_target(spent: int, n_claims: int, per_claim: int) -> int:
    """«Поднять до N»: потрачено + проверка всех непроверенных, с запасом до порога; округление до 10k."""
    need = spent + max(1, n_claims) * per_claim
    thr = float(cost.pricing().get("budget_threshold") or 0.8)
    return int(math.ceil(need / thr / 10000.0) * 10000)


def ask(slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    ref = pending_budget_refusal(slug)
    if not ref:
        raise core.BulochkaError("Ждущего отказа BUDGET нет: бюджет шага не превышен.")
    if ref.get("gate_id"):
        g = core.get("gate", ref["gate_id"], slug)
        if g and g.get("answer") in (None, ""):
            return {"gate": g, "created": False, "refusal": ref}
    claims = core.latest("claim", slug)
    per_claim = x_econ.verify_claim_tokens()
    ids: List[str] = list(ref.get("unverified_ids") or [])
    spent, budget = int(ref.get("spent") or 0), int(ref.get("budget") or 0)
    raise_to = raise_target(spent, len(ids), per_claim)
    options = []
    for cid in ids[:gates.MAX_OPTIONS - 2]:
        vm = (claims.get(cid) or {}).get("verify_minutes")
        mins = f"~{vm:g} мин вашего времени, " if isinstance(vm, (int, float)) else ""
        options.append(f"{cid} — проверить ({mins}~{cost.fmt_tok(per_claim)} ток)")
    raise_label = f"поднять бюджет до {cost.fmt_tok(raise_to)} ток"
    options += [raise_label, STOP_LABEL]
    pct = round(spent * 100 / budget) if budget else None
    question = (f"Бюджет шага «{ref.get('step')}» на исходе: потрачено {pct}% "
                f"({cost.fmt_tok(spent)} из {cost.fmt_tok(budget)} токенов). "
                + (f"Непроверенными остаются {', '.join(ids)}. Что проверить?" if ids else "Что делаем?"))
    outcomes = {o: ("проверяю только его и останавливаюсь" if CLAIM_RE.match(o) else
                    "продолжаю шаг с новым бюджетом" if o == raise_label else
                    "останавливаюсь и пишу квитанцию") for o in options}
    why = "бюджет шага на исходе"
    # `ask` базового слоя принимает до gates.MAX_OPTIONS (8) вариантов: до 6 утверждений + «поднять» + «остановиться»
    g, _, _ = gates.ask("question", question, options, why=why, default=STOP_LABEL, flips=True,
                        claims=[], outcomes=outcomes, step=ref.get("step"), slug=slug)
    g = dict(g)
    g.update({"refusal_id": ref["id"], "budget_raise_to": raise_to, "claim_tokens": per_claim})
    core.put("gate", g, slug)
    r2 = dict(ref)
    r2["gate_id"] = g["id"]
    core.put("refusal", r2, slug)
    return {"gate": g, "created": True, "refusal": r2}


def parse_tokens(text: str) -> Optional[int]:
    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(k|к|тыс\.?|m|м|млн)?", text, re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1).replace(" ", "").replace(",", "."))
    mult = {"k": 1e3, "к": 1e3, "тыс": 1e3, "тыс.": 1e3, "m": 1e6, "м": 1e6, "млн": 1e6}.get(
        (m.group(2) or "").lower(), 1)
    return int(n * mult)


def classify(answer: str) -> Dict[str, Any]:
    a = (answer or "").strip()
    m = CLAIM_RE.search(a)
    if m:
        return {"kind": "claim", "claim": m.group(0)}
    low = a.lower()
    if "подн" in low or "бюджет" in low:
        return {"kind": "raise", "tokens": parse_tokens(a)}
    if any(w in low for w in ("останов", "стоп", "хватит", "отмена")):
        return {"kind": "stop"}
    return {"kind": None}


def resolve(gate_id: str, slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    g = core.get("gate", gate_id, slug)
    if not g or not g.get("refusal_id"):
        raise core.BulochkaError(f"{gate_id} — не ворота бюджета (их создаёт `bulochka budget ask`).")
    if g.get("answer") in (None, ""):
        raise core.BulochkaError(f"{gate_id} ещё без ответа продакта: `bulochka answer {gate_id} --answer «…»`.")
    ref = core.get("refusal", g["refusal_id"], slug)
    if ref is None:
        raise core.BulochkaError(f"Отказ {g['refusal_id']} не найден.")
    dfr = x_econ.load_deferred(slug)
    done = [x for x in dfr.get("resolutions") or [] if x.get("gate_id") == gate_id]
    if done or ref.get("outcome") != "pending":
        return dict(done[-1] if done else {"gate_id": gate_id, "outcome": ref.get("outcome")}, repeated=True)
    choice = classify(g["answer"])
    ids = list(ref.get("unverified_ids") or [])
    step = ref.get("step") or cost.NO_STEP
    budget_before = int(ref.get("budget") or 0)
    res: Dict[str, Any] = {"gate_id": gate_id, "refusal_id": ref["id"], "step": step, "answer": g["answer"],
                           "budget_before": budget_before, "ts": core.now_iso()}
    upd = dict(ref)
    if choice["kind"] == "claim":
        cid = choice["claim"]
        per = int(g.get("claim_tokens") or x_econ.verify_claim_tokens())
        new_budget = budget_before + per
        x_econ.set_step_budget(step, new_budget, slug)
        deferred = [i for i in ids if i != cid]
        res.update({"decision": "claim", "claim": cid, "budget_after": new_budget, "claim_tokens": per,
                    "deferred": deferred, "outcome": "accepted"})
        upd.update({"outcome": "accepted", "resolution": f"продакт выбрал {cid}; остальные отложены ({gate_id})"})
        dfr.setdefault("grants", []).append({"step": step, "claim": cid, "budget": new_budget, "gate_id": gate_id,
                                             "refusal_id": ref["id"], "ts": res["ts"], "closed": False})
    elif choice["kind"] == "raise":
        new_budget = choice.get("tokens") or int(g.get("budget_raise_to") or 0)
        if new_budget < 10000:  # «поднять до 500» без единиц — скорее всего тысячи
            new_budget = int(g.get("budget_raise_to") or 0)
        x_econ.set_step_budget(step, new_budget, slug)
        reason = f"продакт поднял бюджет шага «{step}» до {new_budget} токенов ({gate_id})"
        res.update({"decision": "raise", "budget_after": new_budget, "deferred": [], "outcome": "overridden"})
        upd.update({"outcome": "overridden", "override_reason": reason})
    elif choice["kind"] == "stop":
        res.update({"decision": "stop", "budget_after": budget_before, "deferred": ids, "outcome": "accepted"})
        upd.update({"outcome": "accepted", "resolution": f"продакт остановил шаг ({gate_id})"})
    else:
        raise core.BulochkaError(f"Ответ «{g['answer']}» не распознан: нужен C###, «поднять бюджет до N» или "
                                f"«{STOP_LABEL}».")
    core.put("refusal", upd, slug)
    known = list(dfr.get("deferred") or [])
    for i in res["deferred"]:
        if i not in known:
            known.append(i)
        dfr.setdefault("items", []).append({"id": i, "step": step, "gate_id": gate_id, "refusal_id": ref["id"],
                                            "ts": res["ts"]})
    dfr["deferred"] = known
    dfr.setdefault("resolutions", []).append(res)
    x_econ.save_deferred(dfr, slug)
    journal.event("budget.resolve", step=step if step != cost.NO_STEP else None, actor="agent", slug=slug,
                  gate_id=gate_id, refusal_id=ref["id"], decision=res["decision"], deferred=res["deferred"])
    if res["decision"] == "stop":
        from . import project, x_receipt
        project.write_resume(slug, "pause")
        journal.event("session.pause", step=step if step != cost.NO_STEP else None, actor="agent", slug=slug,
                      output_path="RESUME.md")
        rc = x_receipt.write(slug)
        res["receipt"] = rc["path"]
    return res


def active_grant(step: Optional[str], slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for gr in reversed(x_econ.load_deferred(slug).get("grants") or []):
        if gr.get("step") == step and not gr.get("closed"):
            return gr
    return None


def close_grant(grant: Dict[str, Any], why: str, slug: Optional[str] = None) -> None:
    d = x_econ.load_deferred(slug)
    for gr in d.get("grants") or []:
        if gr.get("gate_id") == grant.get("gate_id") and not gr.get("closed"):
            gr.update({"closed": True, "closed_why": why, "closed_at": core.now_iso()})
    x_econ.save_deferred(d, slug)


# ---------- CLI ----------

def cmd_ask(args) -> int:
    r = ask()
    g = r["gate"]
    human = (f"{g['id']}: {g['question']}\n" + "\n".join(f"  {i}. {o}" for i, o in enumerate(g["options"], 1))
             + ("" if r["created"] else "\n(ворота уже заданы и ждут ответа)"))
    return core.emit("budget", {"gate_id": g["id"], "created": r["created"], "refusal_id": r["refusal"]["id"],
                                "options": g["options"], "question": g["question"]},
                     human=human, as_json=getattr(args, "json", False),
                     next_cmd=f"bulochka answer {g['id']} --answer «…» (дословно продакт), затем bulochka budget resolve {g['id']}")


def cmd_resolve(args) -> int:
    r = resolve(args.gate_id)
    if r.get("repeated"):
        human = f"{args.gate_id} уже исполнен: {r.get('decision') or r.get('outcome')}."
    elif r["decision"] == "claim":
        human = (f"Проверяю только {r['claim']}: бюджет шага {cost.fmt_tok(r['budget_before'])} → "
                 f"{cost.fmt_tok(r['budget_after'])} ток (+{cost.fmt_tok(r['claim_tokens'])} на одну проверку). "
                 f"Отложены: {', '.join(r['deferred']) or '—'}. После проверки — останов и `bulochka receipt`.")
    elif r["decision"] == "raise":
        human = f"Бюджет шага поднят до {cost.fmt_tok(r['budget_after'])} ток по решению продакта. Продолжаю шаг."
    else:
        human = (f"Остановился по решению продакта. Отложены: {', '.join(r['deferred']) or '—'}. "
                 f"Квитанция: {r.get('receipt')}. Продолжить: claude \"продолжай\".")
    return core.emit("budget", r, human=human, as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("budget", help="бюджет шага: выбор продакта при отказе BUDGET")
    bs = p.add_subparsers(dest="budget_cmd")
    a = bs.add_parser("ask", help="ворота по последнему отказу BUDGET: что проверить, поднять бюджет, остановиться")
    a.set_defaults(func=cmd_ask)
    r = bs.add_parser("resolve", help="исполнить ответ продакта на воротах бюджета")
    r.add_argument("gate_id")
    r.set_defaults(func=cmd_resolve)
