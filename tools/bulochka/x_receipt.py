"""Квитанция остановки: разделы «Готово / Наполовину / Уже можно пользоваться / Цена продолжения / Продолжить одной
командой».

«Цена продолжения» (прогон 22.11: в квитанции не было, во что встанет остаток): потрачено к остановке — токены,
% недели (если есть замер), минуты продакта — и остаток по смете на шаги, которые ещё не закрыты. Тот же раздел
встаёт в RESUME.md (project.RESUME_SECTIONS) с полями `spent` и `left_by_smeta`, как ждёт шаблон.

Разделы зарегистрированы в project.RECEIPT_SECTIONS: хук Stop базового слоя (`stop_receipt.py`) сам вставляет их перед
«Далее», в том же процессе. `bulochka receipt` (и `budget resolve` → «остановиться») пишет квитанцию тем же хуком.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core, journal, project

HOOKS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "hooks"
RESUME_CMD = 'claude "продолжай"'


def _write_receipt(slug: str, session_id: Optional[str]) -> None:
    """Квитанция хуком Stop базового слоя (наши разделы он берёт из RECEIPT_SECTIONS); без хука — заголовок и разделы."""
    try:
        if str(HOOKS_DIR) not in sys.path:
            sys.path.insert(0, str(HOOKS_DIR))
        import stop_receipt  # type: ignore
        stop_receipt.handle({"session_id": session_id})
        return
    except Exception:  # noqa: BLE001 — без базовой части квитанция всё равно нужна
        pass
    text = f"# Квитанция остановки — {slug}\n\nВремя: {core.now_iso()}.\n\n" + build_sections(slug)["text"]
    core.atomic_write(core.project_dir(slug) / "STOP_RECEIPT.md", text)


def step_runs(slug: str) -> Dict[str, List[Dict[str, Any]]]:
    done, half = [], []
    for r in journal.by_key(slug).values():
        if r.get("operation") != "step.run":
            continue
        if r.get("status") == "committed":
            done.append(r)
        elif r.get("status") in ("started", "uncertain"):
            half.append(r)
    key = lambda r: r.get("id") or ""  # noqa: E731
    return {"done": sorted(done, key=key), "half": sorted(half, key=key)}


def usable_reports(slug: str, done: List[Dict[str, Any]]) -> Dict[str, Any]:
    """report/*.md, закреплённые commit шага, без ошибок `check`, которые на них ссылаются."""
    pdir = core.project_dir(slug)
    committed = [r.get("output_path") for r in done if r.get("output_path")]
    files = []
    for rel in committed:
        p = (pdir / rel)
        if rel.startswith("report/") and rel.endswith(".md") and p.exists() and rel not in files:
            files.append(rel)
    errors: Optional[List[Dict[str, Any]]] = None
    try:
        from . import verify
        errors = verify.run_check().get("errors") or []
    except Exception:  # noqa: BLE001
        errors = None
    usable, blocked = [], []
    for rel in files:
        if errors is None:
            blocked.append((rel, "проверка `bulochka check` не запустилась"))
            continue
        text = (pdir / rel).read_text(encoding="utf-8")
        hits = []
        for e in errors:
            ids = [i for i in (e.get("ids") or []) if i and re.search(r"\b" + re.escape(str(i)) + r"\b", text)]
            if ids or Path(rel).name in str(e.get("msg") or ""):
                hits.append(f"{e.get('rule')} {', '.join(ids)}".strip())
        if hits:
            blocked.append((rel, "ошибки проверки: " + "; ".join(hits[:3])))
        else:
            usable.append(rel)
    return {"usable": usable, "blocked": blocked, "check_ran": errors is not None}


def continuation_price(slug: str) -> Dict[str, Any]:
    """Потрачено к остановке и остаток по смете на незакрытые шаги."""
    from . import cost, x_econ
    stored = cost.load_cost(slug)
    try:
        d = cost.compute(slug, write=False, writer="receipt")
    except Exception:  # noqa: BLE001 — без транскриптов квитанция всё равно нужна
        d = stored
    if not d.get("tokens") and stored.get("tokens"):  # транскрипты недоступны — последний записанный учёт
        d = stored
    spent = int(d.get("tokens") or 0)
    spent_pct = d.get("weekly_limit_pct_delta")
    minutes = cost.minutes_human(slug)
    est = d.get("estimate") or cost.load_cost(slug).get("estimate") or {}
    res: Dict[str, Any] = {"spent_tokens": spent, "spent_weekly_pct": spent_pct, "spent_minutes": minutes,
                           "estimate_tokens": est.get("tokens"), "left_tokens": None, "left_steps": [],
                           "left_minutes": None, "left_weekly": None}
    if not est.get("tokens"):
        return res
    try:
        done = set(project.compute(slug).get("done_steps") or [])
    except Exception:  # noqa: BLE001
        done = set()
    spent_by_step = x_econ.step_tokens(d)
    by_step = est.get("tokens_by_step") or {}
    left = {}
    for s in est.get("steps") or list(by_step):
        if s in done:
            continue
        left[s] = max(0, int(by_step.get(s) or 0) - int(spent_by_step.get(s) or 0))  # начатый шаг — его остаток
    lt = sum(left.values())
    pm = x_econ.pct_for_tokens(lt) if lt else {"pct": None, "pct_range": None, "rub": None, "rub_range": None}
    res.update({"left_tokens": lt, "left_steps": [s for s in left], "left_by_step": left,
                "left_minutes": round(max(0.0, float(est.get("minutes_human") or 0) - minutes), 1),
                "left_weekly": pm, "over_estimate_pct": (round((spent - est["tokens"]) / float(est["tokens"]) * 100, 1)
                                                         if spent > est["tokens"] else None)})
    return res


def price_section(slug: str) -> str:
    from . import cost, x_econ
    p = continuation_price(slug)
    week = (f"{p['spent_weekly_pct']:g}% недельного лимита" if isinstance(p["spent_weekly_pct"], (int, float))
            else "недельный лимит: замеров нет")
    spent = f"{cost.fmt_tok(p['spent_tokens'])} ток · {week} · {p['spent_minutes']:g} мин продакта"
    lines = ["## Цена продолжения", "", f"- Потрачено к остановке (spent): {spent}"]
    if p["left_tokens"] is None:
        lines.append("- Остаток по смете (left_by_smeta): сметы нет — оценить нельзя "
                     "(`python3 -m tools.bulochka smeta --brief projects/<slug>/order.md`)")
    elif not p["left_steps"]:
        lines.append(f"- Остаток по смете (left_by_smeta): 0 ток — все шаги сметы закрыты "
                     f"(смета {cost.fmt_tok(p['estimate_tokens'])})")
    else:
        pct_txt, rub_txt = x_econ.fmt_pct_money(p["left_weekly"])
        lines.append(f"- Остаток по смете (left_by_smeta): ~{cost.fmt_tok(p['left_tokens'])} ток · {pct_txt} · "
                     f"~{p['left_minutes']:g} мин продакта · {rub_txt}")
        lines.append("  на шаги: " + ", ".join(f"{s} ~{cost.fmt_tok(t)}" for s, t in p["left_by_step"].items()))
    if p.get("over_estimate_pct"):
        lines.append(f"- Смета уже превышена на {p['over_estimate_pct']:g}% — остаток по ней занижен.")
    lines.append("- Возобновление само по себе почти ничего не стоит: новый чат читает RESUME.md и статус, "
                 "сделанное не повторяется.")
    return "\n".join(lines) + "\n"


def build_sections(slug: str) -> Dict[str, Any]:
    from . import x_econ
    runs = step_runs(slug)
    dfr = x_econ.load_deferred(slug)
    outs = usable_reports(slug, runs["done"])
    lines = ["## Готово"]
    lines += [f"- шаг {r.get('step')}: {r.get('output_path') or 'без файла'} ({r['id']})" for r in runs["done"]] \
        or ["- ни один шаг ещё не закрыт"]
    lines += ["", "## Наполовину"]
    half = [f"- шаг {r.get('step')} начат и не закрыт ({r['id']}, {r.get('status')})" for r in runs["half"]]
    for it in dfr.get("items") or []:
        half.append(f"- {it['id']}: проверка отложена по бюджету шага «{it.get('step')}» "
                    f"(решение продакта {it.get('gate_id')})")
    lines += half or ["- ничего"]
    lines += ["", "## Уже можно пользоваться"]
    lines += [f"- `{rel}` — шаг закрыт, ошибок проверки нет" for rel in outs["usable"]] or ["- пока ничего"]
    for rel, why in outs["blocked"]:
        lines.append(f"- `{rel}` — пока нельзя: {why}")
    try:
        lines += [""] + price_section(slug).rstrip("\n").split("\n")
    except Exception as e:  # noqa: BLE001 — без цены квитанция всё равно нужна
        lines += ["", "## Цена продолжения", "", f"- не посчитана: {type(e).__name__}"]
    lines += ["", "## Продолжить одной командой", "", "```", RESUME_CMD, "```", "",
              "Агент сам поднимет состояние через `/resume-run` и продолжит с места остановки."]
    deferred = dfr.get("deferred") or []
    if deferred:
        lines.append(f"Отложенные проверки ({', '.join(deferred)}) вернутся, если поднять бюджет шага: "
                     "`python3 -m tools.bulochka cost --step <шаг> --set-budget <токены>`.")
    return {"text": "\n".join(lines) + "\n", "done": [r["id"] for r in runs["done"]],
            "half": len(half), "usable": outs["usable"], "deferred": deferred}


def receipt_section(slug: str) -> str:
    """project.RECEIPT_SECTIONS: наши разделы квитанции (хук Stop базового слоя ставит их перед «Далее»)."""
    return build_sections(slug)["text"]


project.RECEIPT_SECTIONS.append(receipt_section)
project.RESUME_SECTIONS.append(price_section)


def write(slug: Optional[str] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    sid = session_id or journal.current_session_id(slug)
    _write_receipt(slug, sid)
    sec = build_sections(slug)
    sec["path"] = str(core.project_dir(slug) / "STOP_RECEIPT.md")
    return sec


def cmd_receipt(args) -> int:
    res = write()
    human = (f"Квитанция: {res['path']}\nГотово шагов: {len(res['done'])}; наполовину: {res['half']}; "
             f"уже можно пользоваться: {', '.join(res['usable']) or 'пока ничего'}.\n"
             f"Продолжить: {RESUME_CMD}")
    data = {k: v for k, v in res.items() if k != "text"}
    return core.emit("receipt", data, human=human, as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("receipt", help="квитанция остановки: готово, наполовину, чем пользоваться, как продолжить")
    p.set_defaults(func=cmd_receipt)
