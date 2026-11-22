"""bulochka export — одностраничник из реестров (`--one-pager`) и сборка документов шага в один md (`--pdf-md`).

Одностраничник собирается только из state/*.jsonl и state/cost.json: каждое число в нём — из файлов.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import core, cost, ext

# Разделы одностраничника от расширений: fn(slug) -> str (markdown), встают перед «Чего стоил прогон».
# Модули x_*.py добавляют функции при импорте: export.ONE_PAGER_SECTIONS.append(fn).
ONE_PAGER_SECTIONS: List[Callable[[str], str]] = []

KIND_RU = {"keep": "оставить", "close": "закрыть", "change": "изменить"}
STATUS_RU = {"open": "открыт", "chosen": "выбран", "rejected": "отклонён", "locked": "закреплён",
             "draft": "черновик", "superseded": "заменено", "invalidated": "отменено",
             "revision_proposed": "предложен пересмотр"}
TYPE_TAG = {"fact": "Факт", "inference": "Вывод", "assumption": "Гипотеза", "forecast": "Прогноз",
            "judgment": "Мнение", "synthetic": "Синтетика", "promise": "Обещание", "common_knowledge": "Факт"}


def _cell(s: Any) -> str:
    return str(s if s is not None else "—").replace("|", "\\|").replace("\n", " ")


def _threshold_text(t: Any) -> str:
    """Порог revise_if как написан; неразборчивый — с пометкой (core.parse_threshold)."""
    if t in (None, ""):
        return ""
    try:
        core.parse_threshold(t)
    except core.BulochkaError:
        return f"{t} (порог не числовой — сверка вручную)"
    return str(t)


def build_one_pager(slug: Optional[str] = None) -> str:
    slug = slug or core.current_slug()
    decisions = core.latest("decision", slug)
    options = core.latest("option", slug)
    claims = core.latest("claim", slug)
    objections = core.latest("objection", slug)
    refusals = core.latest("refusal", slug)
    sources = core.latest("source", slug)
    c = cost.load_cost(slug)

    lines: List[str] = [f"# Одностраничник: {slug}", "", f"Собрано {core.now_iso()} из реестров проекта.", ""]
    active_dec = [d for d in decisions.values() if d.get("status") not in ("superseded", "invalidated")]
    if not active_dec:
        lines += ["## Решение", "", "Решений в реестре ещё нет.", ""]
    for d in sorted(active_dec, key=lambda x: x["id"]):
        lines += [f"## {d['id']}. {d.get('question', '')}", ""]
        status = STATUS_RU.get(d.get("status"), d.get("status"))
        chosen = d.get("chosen_option")
        lines.append(f"Статус: {status}" + (f"; выбран {chosen} человеком (ворота {d.get('human_gate_id', '—')})"
                                             if chosen else "; выбор за человеком"))
        if d.get("owner"):
            lines.append(f"Владелец: {d['owner']}. Метрика: {d.get('metric', '—')}, первое чтение {d.get('first_read', '—')}.")
        lines += ["", "| Вариант | Тип | Статус | Возражений |", "|---|---|---|---|"]
        for o in sorted((o for o in options.values() if o.get("decision_id") == d["id"]), key=lambda x: x["id"]):
            n_obj = sum(1 for r in objections.values() if r.get("target_id") == o["id"])
            lines.append(f"| {o['id']} {_cell(o.get('title'))} | {KIND_RU.get(o.get('kind'), o.get('kind'))} | "
                         f"{STATUS_RU.get(o.get('status'), o.get('status', '—'))} | {n_obj} |")
        lines.append("")
        prem = [p.get("claim_id") for p in d.get("premises") or [] if isinstance(p, dict)]
        if prem:
            lines += ["Основания:", ""]
            for cid in prem:
                cl = claims.get(cid) or {}
                src = ", ".join(cl.get("source_ids") or [])
                lines.append(f"- [{TYPE_TAG.get(cl.get('type'), 'Мнение')}] {cl.get('text', cid)} [{cid}"
                             + (f"; {src}" if src else "") + f"; грейд {cl.get('grade', '—')}]")
            lines.append("")
        rv = d.get("revise_if") or []
        if rv:
            lines += ["Решение в силе, пока выполнены условия (иначе — пересмотр):", ""]
            for r in rv:
                lines.append(f"- {r.get('indicator', '—')} {_threshold_text(r.get('threshold'))} — "
                             f"проверить к {r.get('check_by', '—')}")
            lines.append("")

    open_obj = [r for r in objections.values() if r.get("status", "open") == "open"]
    active_claims = [x for x in claims.values() if x.get("status", "active") == "active"]
    lines += ["## Доказательная база", "",
              f"- Источников: {sum(1 for s in sources.values() if s.get('status', 'active') == 'active')}",
              f"- Утверждений: {len(active_claims)} (фактов {sum(1 for x in active_claims if x.get('type') == 'fact')}, "
              f"гипотез {sum(1 for x in active_claims if x.get('type') == 'assumption')})",
              f"- Возражений открыто: {len(open_obj)}" + (" — до экспорта стратегии их нужно разобрать" if open_obj else ""),
              f"- Отказов: {len(refusals)}", ""]

    for sect in ext.render_sections(ONE_PAGER_SECTIONS, slug):
        lines += [sect, ""]

    lines += ["## Чего стоил прогон", ""]
    if c:
        lines.append(f"- Токены: {cost.fmt_tok(c.get('tokens'))} (вход + выход + запись кэша)")
        lines.append(f"- Минуты продакта: {c.get('minutes_human', 0)}")
        if c.get("weekly_limit_pct_delta") is not None:
            lines.append(f"- Недельный лимит подписки: +{c['weekly_limit_pct_delta']:g} п.п. "
                         f"({c.get('weekly_limit_pct_before'):g}% → {c.get('weekly_limit_pct_after'):g}%)")
            lines.append(f"- Из стоимости подписки: ≈{c.get('rub_equiv_total', 0):.0f} ₽ "
                         f"(${c.get('plan_price_month')}/мес, курс {c.get('fx_rate')} ₽/$, на {c.get('plan_price_date')})")
    else:
        lines.append("- Данных о затратах ещё нет: `bulochka cost`.")
    lines.append("")
    return "\n".join(lines)


def build_pdf_md(slug: Optional[str] = None) -> str:
    out = core.project_dir(slug) / "report"
    parts = []
    for p in sorted(out.glob("[0-9]*.md")):
        parts.append(p.read_text(encoding="utf-8").rstrip() + "\n")
    if not parts:
        raise core.BulochkaError("В report/ нет документов шагов (NN_*.md) — собирать нечего.")
    return "\n\n---\n\n".join(parts)


def cmd_export(args) -> int:
    slug = core.current_slug()
    pdir = core.project_dir(slug)
    if not (args.one_pager or args.pdf_md):
        raise core.BulochkaError("Укажите, что собрать: `bulochka export --one-pager` или `--pdf-md`.")
    paths = []
    if args.one_pager:
        p = pdir / "report" / "one_pager.md"
        core.atomic_write(p, build_one_pager(slug))
        paths.append(str(p))
    if args.pdf_md:
        p = pdir / "report" / "strategy_full.md"
        core.atomic_write(p, build_pdf_md(slug))
        paths.append(str(p))
    return core.emit("export", {"paths": paths}, human="Собрано: " + ", ".join(paths), as_json=args.json)


def register(sub) -> None:
    p = sub.add_parser("export", help="одностраничник из реестров или сборка документов в один md")
    p.add_argument("--one-pager", action="store_true", dest="one_pager")
    p.add_argument("--pdf-md", action="store_true", dest="pdf_md")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="машиночитаемый ответ")
    p.set_defaults(func=cmd_export)
