"""Смета до запуска и ошибка прогноза: `bulochka smeta --brief <file>` и `bulochka smeta close`.

Смета учится на истории прогонов (`knowledge/runs/history.jsonl`): медиана токенов на шаг, поиск — вопросы плана ×
токены на вопрос, минуты продакта — 3 ворот × медиана review_seconds + ожидаемые verify_minutes.
Без истории — коэффициенты из config/pricing.json. Доля недели и ₽ — из связи «токены ↔ Δ% недели» в истории.
Если медианная |ошибка| прошлых смет > 50% — диапазон [p25; p75], а не точка.

Режим сбора (прогон 22.11: продакт ответил «только бриф», а в смете осталось 188k на поиск, факт −25%):
- `web` — поиск: вопросы плана × токены на вопрос по прогонам с поиском;
- `brief` — только бриф и файлы продакта: шаг сбора по прогонам без поиска, строки поиска в карточке нет.
Режим — флаг `--no-web`/`--web`, строка брифа «Сбор: только бриф» или ответ продакта «только бриф» на воротах.
`smeta recalc` после утверждения плана: известны число вопросов плана и режим сбора. Ворота сметы
повторяются, только если новая оценка отличается больше чем на `smeta_recalc_gate_pct` % (config/pricing.json).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core, cost, gates, x_econ

GATE_QUESTION = "Запускаем по смете?"
GATE_OPTIONS = ["идём", "урезать", "отмена"]
GATES_PER_RUN = 3
RANGE_IF_ERROR_PCT = 50.0
RECALC_QUESTION = "Запускаем по новой смете?"
RECALC_GATE_PCT = 15.0
TOKENS_PER_QUESTION = 27000   # без прогонов с поиском: шаг сбора B01 — 188k на 7 вопросов
RESEARCH_BRIEF_TOKENS = 25000  # без прогонов «только бриф»: шаг сбора mf-family-devices — 22k
BRIEF_SOURCES_MAX = 2          # прогон без поля collect: ≤2 источников — сбора из сети не было
WEB, BRIEF = "web", "brief"
COLLECT_TXT = {WEB: "бриф и поиск в сети", BRIEF: "только бриф и файлы продакта, сеть не нужна"}
_BRIEF_ONLY = re.compile(r"только\s+бриф|без\s+(?:веб[- ]?)?поиска|сверх\s+брифа\s+нет|без\s+сбора", re.I)
_BRIEF_LINE = re.compile(r"(?im)^\W*(?:\d+\.)?\W*(?P<k>сбор|поиск|доступные\s+данные|данные)\W*?[:.]\W*(?P<v>[^\n]+)$")


def row_collect(r: Dict[str, Any]) -> str:
    """Режим сбора прогона в истории: поле collect или (для старых строк) по числу источников."""
    if r.get("collect") in (WEB, BRIEF):
        return r["collect"]
    if isinstance(r.get("sources"), int) and r["sources"] <= BRIEF_SOURCES_MAX:
        return BRIEF
    return WEB


def collect_mode(slug: Optional[str], brief_text: str = "", override: Optional[str] = None) -> Tuple[str, str]:
    """(режим, основание): флаг → строка брифа «Сбор: …» → ответ продакта на воротах → по умолчанию поиск."""
    if override in (WEB, BRIEF):
        return override, "флаг " + ("--no-web" if override == BRIEF else "--web")
    for m in _BRIEF_LINE.finditer(brief_text or ""):
        v, line = m.group("v"), f"строка брифа «{m.group(0).strip()[:80]}»"
        if m.group("k").lower() in ("сбор", "поиск"):
            return (BRIEF if _BRIEF_ONLY.search(v) or v.strip().lower().startswith("нет") else WEB), line
        if _BRIEF_ONLY.search(v[:40]):  # «Доступные данные: только бриф …»; иначе строка о данных — не о поиске
            return BRIEF, line
    if slug:
        try:
            gs = sorted(core.latest("gate", slug).values(), key=lambda g: g["id"])
        except core.BulochkaError:
            gs = []
        for g in gs:
            if g.get("answered_by") != "human":
                continue
            said = " ".join(str(g.get(k) or "") for k in ("answer", "answer_raw"))
            if _BRIEF_ONLY.search(said):
                return BRIEF, f"ответ продакта на {g['id']}: «{str(g.get('answer_raw') or g.get('answer'))[:80]}»"
    return WEB, "по умолчанию: продакт не говорил «только бриф»"


def estimate(brief_text: str, rows: Optional[List[Dict[str, Any]]] = None, slug: Optional[str] = None,
             steps: Optional[List[str]] = None, collect: str = WEB, questions: Optional[int] = None
             ) -> Dict[str, Any]:
    rows = x_econ.history() if rows is None else rows
    cfg = cost.pricing()
    e = cfg.get("estimate") or cost.DEFAULT_PRICING["estimate"]
    steps = steps or x_econ.steps_of_brief(brief_text)
    by_step: Dict[str, int] = {}
    if rows:
        for s in steps:
            m = x_econ.median([(r.get("tokens_by_step") or {}).get(s) for r in rows
                               if (r.get("tokens_by_step") or {}).get(s)])
            by_step[s] = int(m) if m else int(e["tokens_per_step"])
        tokens = sum(by_step.values())
        basis = f"история прогонов: {len(rows)}"
    else:
        for s in steps:
            by_step[s] = int(e["tokens_per_step"])
        tokens = sum(by_step.values()) + len(brief_text) * int(e["tokens_per_brief_char"])
        basis = "коэффициенты config/pricing.json (истории прогонов нет)"
    # поиск: вопросы плана × токены на вопрос (только прогоны с поиском); «только бриф» — шаг сбора без поиска
    nq = questions if questions is not None else (x_econ.plan_questions(slug) if slug else None)
    if nq is None:
        m = x_econ.median([r.get("questions") for r in rows])
        nq = int(m) if m else x_econ.DEFAULT_QUESTIONS
    search = 0
    if x_econ.SEARCH_STEP in steps:
        old = by_step.get(x_econ.SEARCH_STEP, 0)
        same = [r for r in rows if row_collect(r) == collect]
        if collect == BRIEF:
            m = x_econ.median([(r.get("tokens_by_step") or {}).get(x_econ.SEARCH_STEP) for r in same
                               if (r.get("tokens_by_step") or {}).get(x_econ.SEARCH_STEP)])
            research = int(m) if m else int(e.get("research_brief_tokens", RESEARCH_BRIEF_TOKENS))
        else:
            per_q = x_econ.median([(r.get("tokens_by_step") or {}).get(x_econ.SEARCH_STEP, 0) / r["questions"]
                                   for r in same if r.get("questions")])
            if not per_q and not rows:
                per_q = None  # без истории — коэффициент шага, как раньше
            elif not per_q:
                per_q = float(e.get("tokens_per_question", TOKENS_PER_QUESTION))
            search = int(nq * per_q) if per_q else old
            research = search
        tokens += research - old
        by_step[x_econ.SEARCH_STEP] = research
    # минуты продакта: ворота × медиана review_seconds + ожидаемые verify_minutes
    rs = x_econ.median([r.get("review_seconds_median") for r in rows])
    vm = x_econ.median([r.get("verify_minutes") for r in rows])
    if rs is not None or vm is not None:
        minutes = round(GATES_PER_RUN * (rs or 0) / 60.0 + (vm or 0), 1)
    else:
        minutes = int(e["gates"]) * float(e["minutes_per_gate"])
    pm = x_econ.pct_for_tokens(tokens)
    errs = [abs(r["error_pct"]) for r in rows if isinstance(r.get("error_pct"), (int, float))]
    med_err = x_econ.median(errs)
    rng = None
    if med_err is not None and med_err > RANGE_IF_ERROR_PCT:
        ratios = [r["tokens"] / r["estimate_tokens"] for r in rows if r.get("estimate_tokens") and r.get("tokens")]
        lo, hi = x_econ.quantile(ratios, 0.25), x_econ.quantile(ratios, 0.75)
        if lo is not None:
            rng = [int(tokens * lo), int(tokens * hi)]
    return {
        "tokens": tokens, "range": rng, "median_abs_error_pct": med_err,
        "breakdown": {"model": tokens - search, "search": search, "human": minutes},
        "tokens_by_step": by_step, "steps": steps, "questions": nq, "collect": collect,
        "minutes_human": minutes, "weekly_limit_pct": pm["pct"], "weekly_limit_pct_range": pm["pct_range"],
        "rub_equiv": pm["rub"], "rub_equiv_range": pm["rub_range"], "weekly_basis": pm["basis"],
        "plan_price_date": cfg.get("plan_price_date"), "fx_rate": cfg.get("fx_rate"), "basis": basis,
        "made_at": core.now_iso(), "brief_chars": len(brief_text),
    }


def card(est: Dict[str, Any], brief_label: str) -> str:
    pct_line, rub_line = x_econ.fmt_pct_money({"pct": est["weekly_limit_pct"], "pct_range": est["weekly_limit_pct_range"],
                                                "rub": est["rub_equiv"], "rub_range": est["rub_equiv_range"],
                                                "basis": est["weekly_basis"]})
    if est.get("range"):
        tok = (f"{cost.fmt_tok(est['range'][0])}–{cost.fmt_tok(est['range'][1])} токенов (диапазон: прошлые сметы "
               f"ошибались в среднем на {est['median_abs_error_pct']:.0f}%)")
    else:
        tok = f"~{cost.fmt_tok(est['tokens'])} токенов"
    b = est["breakdown"]
    brief_only = est.get("collect") == BRIEF
    if brief_only:
        gather = (f"- Сбор: {COLLECT_TXT[BRIEF]} ({est.get('collect_basis') or 'режим «только бриф»'}); "
                  f"шаг сбора — в модели")
    else:
        gather = f"- Поиск (вопросов плана: {est['questions']}): ~{cost.fmt_tok(b['search'])} ток"
    recalc = []
    if est.get("recalc_at"):
        recalc = ["", f"Пересчёт после плана ({est['recalc_at'][:10]}): было ~{cost.fmt_tok(est.get('previous_tokens'))}, "
                      f"стало ~{cost.fmt_tok(est['tokens'])} ({est.get('recalc_change_pct', 0):+g}%); "
                      f"вопросов плана {est['questions']}, сбор: {COLLECT_TXT.get(est.get('collect'), '—')}."]
    lines = [f"# Смета до запуска — {brief_label}", "",
             f"- Токены: {tok}",
             f"- {pct_line[0].upper() + pct_line[1:]}",
             f"- Минуты продакта: ~{est['minutes_human']:g}",
             f"- Деньги: {rub_line} (оценка; прайс на {est['plan_price_date']}, курс {est['fx_rate']} ₽/$)",
             "", "## Из чего складывается", "",
             f"- Модель (рассуждение и тексты по шагам): ~{cost.fmt_tok(b['model'])} ток",
             gather,
             f"- Проверка продактом: ~{b['human']:g} мин (ворота и проверка утверждений)",
             "", f"Шаги: {', '.join(est['steps'])}. Основа: {est['basis']}."] + recalc + [
             "", f"**{est.get('gate_question') or GATE_QUESTION}** {' / '.join(GATE_OPTIONS)}", ""]
    return "\n".join(lines)


def find_open_gate(slug: Optional[str]) -> Optional[Dict[str, Any]]:
    for g in sorted(core.latest("gate", slug).values(), key=lambda g: g["id"]):
        if g.get("question") == GATE_QUESTION and g.get("answer") in (None, ""):
            return g
    return None


def make(brief_path: str, slug: Optional[str] = None, collect: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    pdir = core.project_dir(slug)
    bp = Path(brief_path)
    if not bp.is_absolute():
        bp = Path.cwd() / bp
    if not bp.exists():
        raise core.BulochkaError(f"Бриф не найден: {bp}")
    try:  # смета пишется в текущий проект: бриф чужого проекта — значит, сначала `bulochka new` (прогон 22.11, B02)
        rel = bp.resolve().relative_to(core.projects_dir().resolve())
        if rel.parts and rel.parts[0] != slug:
            raise core.BulochkaError(f"Бриф из проекта {rel.parts[0]}, а текущий — {slug}: сначала "
                                    f"`bulochka new <slug> --brief …` для новой задачи, затем смета.")
    except ValueError:
        pass
    text = bp.read_text(encoding="utf-8")
    mode, basis = collect_mode(slug, text, collect)
    est = estimate(text, slug=slug, collect=mode)
    est["collect_basis"] = basis
    est["brief_id"] = x_econ.brief_id(text, str(bp))
    try:
        est["brief_path"] = str(bp.resolve().relative_to(pdir.resolve()))
    except ValueError:
        est["brief_path"] = None
    prev = cost.load_cost(slug)
    prev["estimate"] = est
    x_econ.save_cost(slug, prev)
    out = pdir / "report" / "00_smeta.md"
    core.atomic_write(out, card(est, est["brief_id"] or bp.name))
    g = find_open_gate(slug)
    if g is None:
        g, _, _ = gates.ask("question", GATE_QUESTION, list(GATE_OPTIONS), why="смета до запуска прогона",
                            default="урезать", slug=slug)
    return {"estimate": est, "gate_id": g["id"], "card": str(out)}


def recalc(slug: Optional[str] = None, collect: Optional[str] = None) -> Dict[str, Any]:
    """Пересчёт сметы после утверждения плана: число вопросов плана и режим сбора уже известны.
    made_at не меняется (это id прогона в истории); ворота — только при изменении больше порога."""
    slug = slug or core.current_slug()
    pdir = core.project_dir(slug)
    prev = cost.load_cost(slug)
    old = prev.get("estimate") or {}
    if not old.get("tokens"):
        raise core.BulochkaError("Сметы ещё нет: сначала `bulochka smeta --brief projects/<slug>/order.md`.")
    bp = pdir / (old.get("brief_path") or "order.md")
    text = bp.read_text(encoding="utf-8") if bp.exists() else ""
    mode, basis = collect_mode(slug, text, collect)
    nq = x_econ.plan_questions(slug)
    est = estimate(text, slug=slug, steps=old.get("steps") or None, collect=mode, questions=nq)
    change = round((est["tokens"] - old["tokens"]) / float(old["tokens"]) * 100.0, 1)
    cfg = cost.pricing()
    threshold = float(cfg.get("smeta_recalc_gate_pct", RECALC_GATE_PCT))
    est.update({"made_at": old.get("made_at") or est["made_at"], "recalc_at": core.now_iso(),
                "brief_id": old.get("brief_id"), "brief_path": old.get("brief_path"), "collect_basis": basis,
                "previous_tokens": old["tokens"], "initial_tokens": old.get("initial_tokens") or old["tokens"],
                "recalc_change_pct": change, "plan_questions_known": nq is not None})
    g = None  # ворота прошлого пересчёта ещё ждут ответа — остаются в силе
    for x in sorted(core.latest("gate", slug).values(), key=lambda x: x["id"]):
        if x.get("question") == RECALC_QUESTION and x.get("answer") in (None, ""):
            g = x
    gate_needed = abs(change) > threshold or g is not None
    gate_id = None
    if gate_needed:
        est["gate_question"] = RECALC_QUESTION
        if g is None:
            g, _, _ = gates.ask("question", RECALC_QUESTION, list(GATE_OPTIONS),
                                why=f"смета после плана изменилась на {change:+g}% (порог {threshold:g}%)",
                                default="урезать", slug=slug)
        gate_id = g["id"]
    prev["estimate"] = est
    x_econ.save_cost(slug, prev)
    out = pdir / "report" / "00_smeta.md"
    core.atomic_write(out, card(est, est.get("brief_id") or bp.name))
    from . import journal
    journal.event("smeta.recalc", step=cost.current_step(slug), actor="agent", slug=slug,
                  previous_tokens=old["tokens"], tokens=est["tokens"], change_pct=change, collect=mode,
                  questions=est["questions"], gate_id=gate_id)
    return {"estimate": est, "previous_tokens": old["tokens"], "change_pct": change, "threshold_pct": threshold,
            "gate_needed": gate_needed, "gate_id": gate_id, "card": str(out)}


def run_id(slug: str, cost_data: Dict[str, Any]) -> str:
    made = ((cost_data.get("estimate") or {}).get("made_at")) or "без-сметы"
    return f"{slug}@{made}"


def close(slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    d = cost.compute(slug, writer="smeta")
    est = d.get("estimate") or {}
    gs = [g.get("review_seconds") for g in core.latest("gate", slug).values()
          if isinstance(g.get("review_seconds"), (int, float))]
    claims = [c for c in core.latest("claim", slug).values() if c.get("status", "active") == "active"]
    shown = set()
    for g in core.latest("gate", slug).values():
        if g.get("answer") not in (None, ""):
            shown.update(g.get("claims_shown") or [])
    vmin = sum(float(c.get("verify_minutes") or 0) for c in claims if c["id"] in shown)
    from . import journal
    skipped = [r for r in journal.records(slug) if r.get("operation") == "calc.skipped"]
    tokens = d.get("tokens") or 0
    err = None
    if est.get("tokens"):
        err = round((tokens - est["tokens"]) / float(est["tokens"]) * 100.0, 1)
    row = {
        "run_id": run_id(slug, d), "slug": slug, "brief_id": est.get("brief_id"), "ts": core.now_iso(),
        "tokens": tokens, "tokens_by_step": x_econ.step_tokens(d), "steps": sorted(x_econ.step_tokens(d)),
        "minutes_human": d.get("minutes_human"), "review_seconds_median": x_econ.median(gs),
        "verify_minutes": vmin, "questions": x_econ.plan_questions(slug),
        "sources": sum(1 for _ in core.latest("source", slug)),
        "weekly_delta": d.get("weekly_limit_pct_delta"),
        "estimate": {k: est.get(k) for k in ("tokens", "minutes_human", "weekly_limit_pct", "range")} if est else None,
        "estimate_tokens": est.get("tokens"), "error_pct": err, "calc_skipped": len(skipped),
        "estimate_initial_tokens": est.get("initial_tokens") or est.get("tokens"),
        "collect": est.get("collect") or row_collect({"sources": sum(1 for _ in core.latest("source", slug))}),
    }
    added = x_econ.append_history(row)
    return {"added": added, "row": row}


def cmd_smeta(args) -> int:
    as_json = getattr(args, "json", False)
    if args.action == "close":
        r = close()
        row = r["row"]
        if row["estimate_tokens"]:
            human = (f"Смета {cost.fmt_tok(row['estimate_tokens'])} против факта {cost.fmt_tok(row['tokens'])}: "
                     f"ошибка {row['error_pct']:+g}%; следующая смета учтёт.")
        else:
            human = f"Факт {cost.fmt_tok(row['tokens'])} ток записан в историю; сметы у прогона не было."
        if not r["added"]:
            human += " (прогон уже в истории — повтор не записан)"
        return core.emit("smeta", r, human=human, as_json=as_json)
    collect = BRIEF if args.no_web else (WEB if args.web else None)
    if args.action == "recalc":
        r = recalc(collect=collect)
        est = r["estimate"]
        human = (f"Смета после плана: ~{cost.fmt_tok(r['previous_tokens'])} → ~{cost.fmt_tok(est['tokens'])} ток "
                 f"({r['change_pct']:+g}%; вопросов плана {est['questions']}, сбор: {COLLECT_TXT[est['collect']]}). ")
        if r["gate_needed"]:
            human += (f"Изменение больше {r['threshold_pct']:g}% — ворота {r['gate_id']}: «{RECALC_QUESTION}» "
                      f"{' / '.join(GATE_OPTIONS)}.")
        else:
            human += f"В пределах {r['threshold_pct']:g}% — ворота не повторяю, карточка report/00_smeta.md обновлена."
        data = {k: v for k, v in r.items() if k != "estimate"}
        data.update({"tokens": est["tokens"], "collect": est["collect"], "questions": est["questions"],
                     "breakdown": est["breakdown"]})
        return core.emit("smeta", data, human=human, as_json=as_json,
                         next_cmd=(f"bulochka answer {r['gate_id']} --answer «…» (дословно продакт)"
                                   if r["gate_needed"] else None))
    if not args.brief:
        raise core.BulochkaError("Нужен бриф: `bulochka smeta --brief <file>`, `bulochka smeta recalc` "
                                 "или `bulochka smeta close`.")
    r = make(args.brief, collect=collect)
    data = dict(r["estimate"])
    data["gate_id"] = r["gate_id"]
    data["card"] = r["card"]
    human = Path(r["card"]).read_text(encoding="utf-8") + f"\nВорота {r['gate_id']}: {GATE_QUESTION}"
    return core.emit("smeta", data, human=human, as_json=as_json,
                     next_cmd=f"bulochka answer {r['gate_id']} --answer «…» (дословно продакт)")


def register(sub) -> None:
    p = sub.add_parser("smeta", help="смета прогона до запуска (по истории); recalc — после плана; close — факт в историю")
    p.add_argument("action", nargs="?", choices=("recalc", "close"),
                   help="recalc — пересчёт после утверждения плана; close — записать факт прогона в историю")
    p.add_argument("--brief", help="файл брифа")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-web", dest="no_web", action="store_true", help="сбор только из брифа и файлов продакта")
    g.add_argument("--web", action="store_true", help="сбор с поиском в сети (переопределить «только бриф»)")
    p.set_defaults(func=cmd_smeta)
