"""Тест на разворот перед расчётом: `bulochka flip …` и `bulochka flip report`.

Считать ли? Если правдоподобный диапазон величины целиком по одну сторону порога переворота, расчёт не меняет
выбор ни при каком исходе — пропускаем (`calc.skipped` в журнале, экономия в токенах и ₽ из подписки).
Иначе — считаем и заранее показываем оба исхода (outcomes_preview для ворот).
Каждый `flip …` сам пересобирает `report/flip_tests.md` (прогон 22.11: тест был дважды, а отчёта не было —
`flip report` в путь шага не попал).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core, cost, journal, numbers, x_econ

CALC = Path(__file__).resolve().parents[2] / "scripts" / "calc.py"
UNIT_TXT = {"pp": " п.п.", "share": "%", "RUB": " ₽", "USD": " $", "count": "", "year": ""}


def _num(text: str) -> Optional[Dict[str, Any]]:
    ns = numbers.extract(text or "")
    return ns[0] if ns else None


def parse_switch(text: str) -> Dict[str, Any]:
    n = _num(text)
    if n is None:
        raise core.BulochkaError(f"--switch: в «{text}» нет числа (пример: «12 п.п.»).")
    return n


def parse_range(text: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    parts = re.split(r"\s*(?:\.\.|…|–|—|\s-\s|\bдо\b)\s*", (text or "").strip(), maxsplit=1)
    if len(parts) != 2:
        raise core.BulochkaError(f"--plausible: нужен диапазон «мин..макс», сейчас «{text}».")
    lo, hi = _num(parts[0]), _num(parts[1])
    if lo is None or hi is None:
        raise core.BulochkaError(f"--plausible: в «{text}» нужны два числа.")
    if lo["unit"] == "count" and hi["unit"] != "count":  # «0..3 п.п.» — единица у второго числа
        lo = dict(lo, unit=hi["unit"], value=lo["value"] / 100.0 if hi["unit"] == "share" else lo["value"])
    if lo["value"] > hi["value"]:
        lo, hi = hi, lo
    return lo, hi


def _same_scale(switch: Dict[str, Any], v: Dict[str, Any]) -> float:
    if switch["unit"] == "share" and v["unit"] == "count":
        return v["value"] / 100.0
    if switch["unit"] == "count" and v["unit"] == "share":
        return v["value"] * 100.0
    return float(v["value"])


def show(v: float, unit: str) -> str:
    if unit == "share":
        return f"{v * 100:g}%"
    return f"{v:g}{UNIT_TXT.get(unit, '')}"


def show_range(lo: float, hi: float, unit: str) -> str:
    a, b = show(lo, unit), show(hi, unit)
    suffix = UNIT_TXT.get(unit, "") if unit != "share" else "%"
    if suffix and a.endswith(suffix):
        a = a[: -len(suffix)]
    return f"{a.strip()}–{b}"


def run_calc(spec: str) -> Dict[str, Any]:
    argv = shlex.split(spec)
    if "--json" not in argv:
        argv.append("--json")
    r = subprocess.run([sys.executable, str(CALC)] + argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise core.BulochkaError(f"calc.py {spec}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout)


def decide(question: str, switch_txt: str, plausible_txt: str, *, direction: str = "ge",
           if_above: str = "решение меняется", if_below: str = "решение не меняется",
           calc: Optional[str] = None, slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    core.project_dir(slug)
    sw = parse_switch(switch_txt)
    lo_n, hi_n = parse_range(plausible_txt)
    t = float(sw["value"])
    lo, hi = _same_scale(sw, lo_n), _same_scale(sw, hi_n)
    unit = sw["unit"] if sw["unit"] != "count" else hi_n["unit"]
    sign = "≥" if direction == "ge" else "≤"
    # исход «выше порога» (для ge) включён, если величина может достичь порога
    crosses = (lo < t <= hi) if direction == "ge" else (lo <= t < hi)
    res: Dict[str, Any] = {"question": question, "switch": show(t, unit), "switch_value": t, "direction": direction,
                           "plausible": show_range(lo, hi, unit), "plausible_values": [lo, hi]}
    if calc:
        c = run_calc(calc)
        r = c.get("result") or {}
        res["calc"] = {"command": c.get("command"), "plan_hash": c.get("plan_hash"),
                       "mde_pp": round(float(r["abs_diff"]) * 100, 2) if "abs_diff" in r else None,
                       "n_per_variant": r.get("n_per_variant")}
    th_txt = f"чтобы решение сменилось, нужен эффект {sign}{show(t, unit)}, правдоподобный — {show_range(lo, hi, unit)}"
    if crosses:
        res.update({"decision": "run", "outcomes": {f"{sign}{show(t, unit)}": if_above,
                                                    f"иначе": if_below}})
        res["phrase"] = (f"Расчёт «{question}» может сменить выбор: {th_txt}; считаю. "
                         f"Если {sign}{show(t, unit)} → {if_above}, иначе → {if_below}.")
        journal.event("flip.run", step=cost.current_step(slug), actor="agent", slug=slug, question=question,
                      switch=res["switch"], plausible=res["plausible"])
    else:
        prev = [r for r in journal.records(slug) if r.get("operation") == "calc.skipped"
                and r.get("question") == question and r.get("switch") == res["switch"]
                and r.get("plausible") == res["plausible"]]
        always = if_above if ((direction == "ge" and lo >= t) or (direction == "le" and hi <= t)) else if_below
        saved = x_econ.calc_tokens_estimate()
        pm = x_econ.pct_for_tokens(saved)
        _, rub_txt = x_econ.fmt_pct_money(pm)
        res.update({"decision": "skip", "outcome": always, "saved_tokens": saved, "saved_rub": pm["rub"],
                    "saved_rub_range": pm["rub_range"]})
        money = rub_txt
        res["phrase"] = (f"Расчёт «{question}» не меняет выбор ни при каком исходе: {th_txt}; "
                         f"пропускаю, экономия ≈{cost.fmt_tok(saved)} ток ({money}). Итог при любом исходе: {always}.")
        res["repeated"] = bool(prev)
        if not prev:  # тот же вопрос с тем же порогом и диапазоном — экономия уже учтена
            journal.event("calc.skipped", step=cost.current_step(slug), actor="agent", slug=slug,
                          question=question, switch=res["switch"], plausible=res["plausible"],
                          saved_tokens=saved, saved_rub=pm["rub"], saved_rub_range=pm["rub_range"],
                          calc=res.get("calc"))
    if res.get("calc") and res["calc"].get("mde_pp") is not None:
        res["phrase"] += f" MDE по calc.py: {res['calc']['mde_pp']:g} п.п. на {res['calc']['n_per_variant']} на вариант."
    try:
        res["report"] = report(slug)["path"]
    except (OSError, core.BulochkaError) as e:  # отчёт — побочный продукт, решение «считать ли» уже принято
        res["report_error"] = f"{type(e).__name__}: {e}"
    return res


def report(slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    recs = journal.records(slug)
    skipped = [r for r in recs if r.get("operation") == "calc.skipped"]
    ran = [r for r in recs if r.get("operation") == "flip.run"]
    saved = sum(int(r.get("saved_tokens") or 0) for r in skipped)
    pm = x_econ.pct_for_tokens(saved) if saved else {"pct": None, "pct_range": None, "rub": None, "rub_range": None}
    pct_txt, rub_txt = x_econ.fmt_pct_money(pm)
    lines = ["# Тест на разворот: что не стали считать", "",
             f"- Расчётов пропущено: {len(skipped)}; посчитано после проверки: {len(ran)}",
             f"- Сэкономлено: ≈{cost.fmt_tok(saved)} ток · {pct_txt} · {rub_txt}", ""]
    if skipped:
        lines += ["| Вопрос | Порог переворота | Правдоподобно | Экономия, ток |", "|---|---|---|---|"]
        lines += [f"| {r.get('question')} | {r.get('switch')} | {r.get('plausible')} | "
                  f"{cost.fmt_tok(r.get('saved_tokens'))} |" for r in skipped]
    if ran:
        lines += ["", "Посчитано (порог внутри правдоподобного диапазона):"]
        lines += [f"- {r.get('question')}: порог {r.get('switch')}, правдоподобно {r.get('plausible')}"
                  for r in ran]
    out = core.project_dir(slug) / "report" / "flip_tests.md"
    core.atomic_write(out, "\n".join(lines) + "\n")
    return {"skipped": len(skipped), "ran": len(ran), "saved_tokens": saved, "saved_rub": pm["rub"],
            "path": str(out)}


def cmd_flip(args) -> int:
    as_json = getattr(args, "json", False)
    if args.action == "report":
        r = report()
        return core.emit("flip", r, human=Path(r["path"]).read_text(encoding="utf-8"), as_json=as_json)
    if not (args.switch and args.plausible):
        raise core.BulochkaError("Нужны --switch «порог» и --plausible «мин..макс» (или `bulochka flip report`).")
    r = decide(args.question or "расчёт", args.switch, args.plausible, direction=args.direction,
               if_above=args.if_above, if_below=args.if_below, calc=args.calc)
    return core.emit("flip", r, human=r["phrase"], as_json=as_json)


def register(sub) -> None:
    p = sub.add_parser("flip", help="тест на разворот: считать ли расчёт, который может не менять выбор")
    p.add_argument("action", nargs="?", choices=("report",), help="report — report/flip_tests.md")
    p.add_argument("--question", help="что собираемся считать")
    p.add_argument("--switch", help="порог переворота решения, например «12 п.п.»")
    p.add_argument("--plausible", help="правдоподобный диапазон, например «0..3 п.п.»")
    p.add_argument("--direction", choices=("ge", "le"), default="ge", help="решение меняется при ≥ (ge) или ≤ (le) порога")
    p.add_argument("--if-above", dest="if_above", default="решение меняется", help="исход за порогом, например O2")
    p.add_argument("--if-below", dest="if_below", default="решение не меняется", help="исход до порога, например O1")
    p.add_argument("--calc", help="подкоманда calc.py для MDE, например «mde --base 0.04 --lift 0.1»")
    p.set_defaults(func=cmd_flip)
