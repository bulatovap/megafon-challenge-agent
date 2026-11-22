"""Ценник решения: `bulochka pricetag [D###]` → report/90_price_tag.md.

Первые три строки — то, что измерено: токены, % недельного лимита, минуты продакта. Рубли — четвёртой строкой,
«из стоимости подписки» (оценка). Минуты продакта = ворота (review_seconds) + проверка утверждений, которые
продакт видел на отвеченных воротах (verify_minutes). Минуты человека в рубли не переводим.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core, cost, journal, x_econ


def human_minutes(slug: Optional[str] = None) -> Dict[str, Any]:
    gates = core.latest("gate", slug)
    claims = core.latest("claim", slug)
    secs = sum(g["review_seconds"] for g in gates.values() if isinstance(g.get("review_seconds"), (int, float)))
    shown: List[str] = []
    for g in sorted(gates.values(), key=lambda g: g["id"]):
        if g.get("answer") in (None, ""):
            continue
        for cid in g.get("claims_shown") or []:
            if cid not in shown and cid in claims:
                shown.append(cid)
    verify = 0.0
    for cid in shown:
        c = claims[cid]
        if c.get("material") is False:
            continue
        vm = c.get("verify_minutes")
        if not isinstance(vm, (int, float)):
            from . import claims as claims_mod
            vm = claims_mod.VERIFY_MINUTES.get(c.get("type"), 0)
        verify += float(vm)
    gates_min = secs / 60.0
    return {"gates_minutes": round(gates_min, 1), "verify_minutes": round(verify, 1),
            "minutes_human": round(gates_min + verify, 1), "claims_checked": shown}


def build(decision_id: Optional[str] = None, slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    d = cost.compute(slug, writer="pricetag")
    by = x_econ.step_tokens(d)
    tokens = int(d.get("tokens") or 0)
    search = by.get(x_econ.SEARCH_STEP, 0)
    check = by.get(x_econ.CHECK_STEP, 0)
    synth = max(0, tokens - search - check)
    hm = human_minutes(slug)
    delta = d.get("weekly_limit_pct_delta")
    est = d.get("estimate") or {}
    skipped = [r for r in journal.records(slug) if r.get("operation") == "calc.skipped"]
    saved = sum(int(r.get("saved_tokens") or 0) for r in skipped)
    title = f"# Ценник решения {decision_id}" if decision_id else "# Ценник прогона"
    lines = [title, "",
             f"- Токены: {cost.fmt_tok(tokens)} (поиск {cost.fmt_tok(search)} · проверка {cost.fmt_tok(check)} · "
             f"синтез {cost.fmt_tok(synth)})"]
    if delta is not None:
        lines.append(f"- Недельный лимит: +{delta:g}% ({d['weekly_limit_pct_before']:g}% → "
                     f"{d['weekly_limit_pct_after']:g}%; замер по аккаунту)")
    else:
        lines.append("- Недельный лимит: замеров нет")
    lines.append(f"- Минуты продакта: {hm['minutes_human']:g} (ворота {hm['gates_minutes']:g} + проверка "
                 f"{len(hm['claims_checked'])} утверждений {hm['verify_minutes']:g})")
    if delta is not None and d.get("rub_equiv_total") is not None:
        lines.append(f"- ≈{d['rub_equiv_total']:.0f} ₽ {x_econ.SUB_NOTE} (оценка: ${d['plan_price_month']}/мес × 12/52 × "
                     f"Δ% × {d['fx_rate']} ₽/$, прайс на {d['plan_price_date']})")
    else:
        lines.append("- ₽ из стоимости подписки: замеров нет — рубли не считаю")
    if est.get("tokens"):
        fe = d.get("forecast_error_pct")
        lines.append(f"- Смета против факта: {cost.fmt_tok(est['tokens'])} → {cost.fmt_tok(tokens)} ток"
                     + (f", ошибка прогноза {fe:+g}%" if fe is not None else ""))
    else:
        lines.append("- Смета против факта: сметы не было")
    if skipped:
        lines.append(f"- Тест на разворот: пропущено расчётов {len(skipped)}, экономия ≈{cost.fmt_tok(saved)} ток")
    lines += ["", "Минуты продакта в рубли не переводятся: это время человека, а не доля подписки."]
    out = core.project_dir(slug) / "report" / "90_price_tag.md"
    core.atomic_write(out, "\n".join(lines) + "\n")
    data = {"decision_id": decision_id, "tokens_total": tokens, "tokens_search": search, "tokens_check": check,
            "tokens_synthesis": synth, "weekly_limit_pct_delta": delta, "rub_equiv": d.get("rub_equiv_total"),
            "minutes_human": hm["minutes_human"], "minutes_gates": hm["gates_minutes"],
            "minutes_verify": hm["verify_minutes"], "claims_checked": hm["claims_checked"],
            "estimate_tokens": est.get("tokens"), "forecast_error_pct": d.get("forecast_error_pct"),
            "calc_skipped": len(skipped), "saved_tokens": saved, "path": str(out)}
    if decision_id:
        dec = core.get("decision", decision_id, slug)
        if dec is None:
            raise core.BulochkaError(f"Решение {decision_id} не найдено.")
        if dec.get("status") == "locked":
            pass  # зафиксированное решение не правим (E19): цена — в 90_price_tag.md и cost.json
        elif dec.get("minutes_human") != hm["minutes_human"] or dec.get("tokens_total") != tokens:
            upd = dict(dec)
            upd.update({"minutes_human": hm["minutes_human"], "tokens_total": tokens, "actor": "agent"})
            core.put("decision", upd, slug)
    return data


def cmd_pricetag(args) -> int:
    r = build(args.decision_id)
    return core.emit("pricetag", r, human=Path(r["path"]).read_text(encoding="utf-8"),
                     as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("pricetag", help="ценник решения: токены, %% недели, минуты продакта, ₽ из подписки")
    p.add_argument("decision_id", nargs="?", help="D### — записать minutes_human и tokens_total решению")
    p.set_defaults(func=cmd_pricetag)
