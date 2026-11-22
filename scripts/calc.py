#!/usr/bin/env python3
"""Воспроизводимые расчёты стратега: то, что нельзя считать «в уме».

Подкоманды:
  mde     — выборка на вариант для двустороннего z-теста долей и срок теста;
  srm     — проверка пропорции групп (χ², 1 степень свободы);
  cohort  — таблица удержания по когортам из CSV, разрыв сегментов, форма кривой;
  ltv     — LTV = ARPU · маржа / отток, LTV/CAC;
  schet   — счёт по числам брифа: выражение с именами (`--var plan=5000 --var fact=4000`), формула,
            подстановка и результат. Только числа, + − * / ** // %, скобки, min/max/round/sum/abs.

Каждый вывод содержит входы и `plan_hash` = sha256(команда + входы + версия скрипта).
Его передают в `python3 -m tools.bulochka claim add --type inference --calc --plan-hash <hash> --from C###,… --text "…"`.

Запуск из корня продукта: `python3 scripts/calc.py mde --base 0.04 --lift 0.10 [--json]`.
Только stdlib, Python 3.9+.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Tuple

VERSION = "calc/1.0"
EXIT_OK = 0
EXIT_USAGE = 3

SCRIPT_PATH = Path(__file__).resolve()


class CalcError(Exception):
    """Неверные входы (exit 3)."""


# ---------------------------------------------------------------- общие части

def plan_hash(command: str, inputs: Dict[str, Any]) -> str:
    payload = json.dumps({"command": command, "inputs": inputs, "version": VERSION},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _script_sha256() -> str:
    try:
        return hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def _envelope(command: str, inputs: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    h = plan_hash(command, inputs)
    return {
        "command": command,
        "version": VERSION,
        "inputs": inputs,
        "result": result,
        "plan_hash": h,
        "script_sha256": _script_sha256(),
        "claim_hint": ("python3 -m tools.bulochka claim add --type inference --calc "
                       f"--plan-hash {h} --from C###,… --text \"…\""),
    }


def fmt_int(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%".replace(".", ",")


def fmt_num(x: float, digits: int = 2) -> str:
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def _share(name: str, v: float, allow_one: bool = False) -> float:
    hi_ok = v <= 1 if allow_one else v < 1
    if not (0 < v and hi_ok):
        raise CalcError(f"{name}={v}: ожидается доля в (0; 1), например 0.04 для 4%")
    return v


# ---------------------------------------------------------------- mde

def sample_size_two_props(base: float, lift: float, alpha: float = 0.05, power: float = 0.8,
                          relative: bool = True) -> Dict[str, Any]:
    """n на вариант ≈ (z₁₋α/₂ + z₁₋β)² · (p₁(1−p₁) + p₂(1−p₂)) / (p₂ − p₁)²."""
    _share("base", base)
    _share("alpha", alpha)
    _share("power", power)
    if lift == 0:
        raise CalcError("lift=0: эффект должен быть ненулевым")
    p1 = base
    p2 = base * (1 + lift) if relative else base + lift
    if not 0 < p2 < 1:
        raise CalcError(f"целевая доля {p2:.4f} вне (0; 1): проверь base и lift")
    nd = NormalDist()
    z_a = nd.inv_cdf(1 - alpha / 2)
    z_b = nd.inv_cdf(power)
    n_raw = (z_a + z_b) ** 2 * (p1 * (1 - p1) + p2 * (1 - p2)) / (p2 - p1) ** 2
    return {"p1": p1, "p2": round(p2, 10), "abs_diff": round(p2 - p1, 10),
            "rel_lift": round((p2 - p1) / p1, 10), "z_alpha": round(z_a, 6), "z_beta": round(z_b, 6),
            "n_per_variant_raw": round(n_raw, 2), "n_per_variant": int(math.ceil(n_raw))}


def duration(n_per_variant: int, daily: float, variants: int = 2, min_weeks: int = 2) -> Dict[str, Any]:
    if daily <= 0:
        raise CalcError("daily должен быть > 0 (поток участников в день на весь тест)")
    total = n_per_variant * variants
    days_raw = int(math.ceil(total / daily))
    weeks = max(min_weeks, int(math.ceil(days_raw / 7)))
    return {"total_sample": total, "days_raw": days_raw, "weeks": weeks, "days": weeks * 7,
            "min_weeks": min_weeks, "floor_applied": weeks * 7 > int(math.ceil(days_raw / 7)) * 7}


def cmd_mde(a: argparse.Namespace) -> Dict[str, Any]:
    inputs = {"base": a.base, "lift": a.lift, "lift_type": a.lift_type, "alpha": a.alpha,
              "power": a.power, "daily": a.daily, "variants": a.variants}
    res = sample_size_two_props(a.base, a.lift, a.alpha, a.power, relative=(a.lift_type == "rel"))
    if a.daily is not None:
        if a.variants < 2:
            raise CalcError("variants ≥ 2")
        res["duration"] = duration(res["n_per_variant"], a.daily, a.variants)
    return _envelope("mde", inputs, res)


def human_mde(d: Dict[str, Any]) -> str:
    i, r = d["inputs"], d["result"]
    lines = [
        "Размер выборки (двусторонний z-тест долей)",
        f"  база {fmt_pct(r['p1'], 2)} → цель {fmt_pct(r['p2'], 2)} "
        f"(лифт {fmt_pct(r['rel_lift'], 1)} отн., {fmt_num(r['abs_diff'] * 100, 2)} п.п.)",
        f"  α = {i['alpha']}, мощность = {i['power']}",
        f"  на вариант: {fmt_int(r['n_per_variant'])} (точно {fmt_num(r['n_per_variant_raw'], 1)})",
    ]
    du = r.get("duration")
    if du:
        note = " (поднято до минимума 2 полных недели)" if du["floor_applied"] else ""
        lines.append(f"  всего {fmt_int(du['total_sample'])} при потоке {fmt_int(i['daily'])}/день → "
                     f"{du['days_raw']} дн. набора → {du['weeks']} нед. = {du['days']} дн.{note}")
    else:
        lines.append("  срок: укажи --daily N (поток участников в день), чтобы посчитать длительность")
    return "\n".join(lines)


# ---------------------------------------------------------------- srm

def srm_test(a_n: int, b_n: int, ratio: float = 0.5, threshold: float = 0.001) -> Dict[str, Any]:
    if a_n < 0 or b_n < 0 or a_n + b_n == 0:
        raise CalcError("a и b — неотрицательные, сумма > 0")
    _share("ratio", ratio)
    total = a_n + b_n
    ea, eb = total * ratio, total * (1 - ratio)
    chi2 = (a_n - ea) ** 2 / ea + (b_n - eb) ** 2 / eb
    p = math.erfc(math.sqrt(chi2 / 2))  # χ² с 1 степенью свободы
    return {"total": total, "expected_a": round(ea, 2), "expected_b": round(eb, 2),
            "observed_share_a": round(a_n / total, 6), "chi2": round(chi2, 6), "p_value": p,
            "threshold": threshold, "srm": p < threshold}


def cmd_srm(a: argparse.Namespace) -> Dict[str, Any]:
    inputs = {"a": a.a, "b": a.b, "ratio": a.ratio, "threshold": a.threshold}
    return _envelope("srm", inputs, srm_test(a.a, a.b, a.ratio, a.threshold))


def human_srm(d: Dict[str, Any]) -> str:
    i, r = d["inputs"], d["result"]
    verdict = ("SRM: группы разделились не в задуманной пропорции — результатам теста не доверяем, ищем ошибку раздачи"
               if r["srm"] else "SRM не обнаружен: пропорция групп в пределах случайности")
    return "\n".join([
        "Проверка пропорции групп (SRM, χ², 1 ст. св.)",
        f"  A = {fmt_int(i['a'])}, B = {fmt_int(i['b'])}, задумано A:B = {i['ratio']}:{round(1 - i['ratio'], 6)}",
        f"  ожидалось A = {fmt_num(r['expected_a'], 1)}, фактическая доля A = {fmt_pct(r['observed_share_a'], 2)}",
        f"  χ² = {fmt_num(r['chi2'], 3)}, p = {r['p_value']:.3g} (порог {i['threshold']})",
        f"  {verdict}",
    ])


# ---------------------------------------------------------------- cohort

def _read_csv(path: Path) -> Tuple[List[Dict[str, str]], str]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines))))
    return rows, hashlib.sha256(raw).hexdigest()


def _num(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    v = v.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if v == "" or v.upper() in ("NA", "N/A", "NULL", "-", "—"):
        return None
    return float(v)


def _month_index(m: str) -> Optional[int]:
    try:
        y, mo = m.strip()[:7].split("-")
        return int(y) * 12 + int(mo) - 1
    except (ValueError, AttributeError):
        return None


def _month_name(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def classify_shape(curve: List[Optional[float]], flat_pp: float = 2.0, rise_pp: float = 1.0) -> Dict[str, Any]:
    """Форма кривой удержания по средним M0..Mk.

    Правило (простое, задокументированное):
      улыбка — после минимума кривая поднимается больше чем на rise_pp п.п.;
      плато  — два последних шага падают меньше чем на flat_pp п.п. каждый и уровень > 5%;
      ведро  — иначе (кривая продолжает течь к нулю);
      мало данных — меньше 4 точек (M0..M3).
    """
    pts = [(k, v) for k, v in enumerate(curve) if v is not None]
    if len(pts) < 4:
        return {"shape": "мало данных", "reason": f"точек {len(pts)} < 4 (нужно M0..M3)"}
    vals = [v for _, v in pts]
    lo = min(range(len(vals)), key=lambda j: vals[j])
    rise = (max(vals[lo:]) - vals[lo]) * 100
    if lo < len(vals) - 1 and rise > rise_pp:
        return {"shape": "улыбка", "reason": f"после минимума M{pts[lo][0]} подъём на {rise:.1f} п.п. > {rise_pp}"}
    d1 = (vals[-3] - vals[-2]) * 100
    d2 = (vals[-2] - vals[-1]) * 100
    if d1 < flat_pp and d2 < flat_pp and vals[-1] > 0.05:
        return {"shape": "плато",
                "reason": f"два последних шага −{d1:.1f} и −{d2:.1f} п.п. < {flat_pp}, уровень {vals[-1] * 100:.1f}%"}
    return {"shape": "ведро",
            "reason": f"два последних шага −{d1:.1f} и −{d2:.1f} п.п.; кривая продолжает падать "
                      f"(M{pts[-1][0]} = {vals[-1] * 100:.1f}%)"}


def cohort_analysis(rows: List[Dict[str, str]], gap_pp: float = 20.0, flat_pp: float = 2.0) -> Dict[str, Any]:
    if not rows:
        raise CalcError("CSV пуст")
    cols = list(rows[0].keys())
    for need in ("cohort", "paid"):
        if need not in cols:
            raise CalcError(f"в CSV нет колонки «{need}»; ожидается cohort,[segment,]trials,paid,m1..mK")
    mcols = sorted([c for c in cols if c and c.lower().startswith("m") and c[1:].isdigit()],
                   key=lambda c: int(c[1:]))
    if not mcols:
        raise CalcError("в CSV нет колонок m1..mK")
    kmax = int(mcols[-1][1:])
    has_seg = "segment" in cols
    has_trials = "trials" in cols

    table: List[Dict[str, Any]] = []
    holes: List[Dict[str, Any]] = []
    agg: Dict[str, Dict[str, Any]] = {}
    seg_months: Dict[str, List[int]] = {}
    overall: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seg = (r.get("segment") or "все").strip() if has_seg else "все"
        coh = (r.get("cohort") or "").strip()
        paid = _num(r.get("paid"))
        trials = _num(r.get("trials")) if has_trials else None
        mi = _month_index(coh)
        if mi is not None:
            seg_months.setdefault(seg, []).append(mi)
        vals = [_num(r.get(f"m{k}")) for k in range(1, kmax + 1)]
        ret: List[Optional[float]] = [1.0 if paid else None]
        for v in vals:
            ret.append(v / paid if (v is not None and paid) else None)
        last_filled = max([k for k, v in enumerate(vals, start=1) if v is not None], default=0)
        for k, v in enumerate(vals, start=1):
            if v is None and k < last_filled:
                holes.append({"segment": seg, "cohort": coh, "month": f"M{k}"})
        conv = (paid / trials) if (paid is not None and trials) else None
        table.append({"segment": seg, "cohort": coh, "trials": trials, "paid": paid,
                      "trial_to_paid": None if conv is None else round(conv, 4),
                      "retention": [None if x is None else round(x, 4) for x in ret]})
        a = agg.setdefault(seg, {"trials": 0.0, "paid_conv": 0.0, "num": [0.0] * (kmax + 1),
                                 "den": [0.0] * (kmax + 1)})
        if trials and paid is not None:
            a["trials"] += trials
            a["paid_conv"] += paid
        if paid:
            series = [paid] + vals
            for k in range(1, kmax + 1):
                if series[k] is not None and series[k - 1] is not None:
                    a["num"][k] += series[k]
                    a["den"][k] += series[k - 1]
            if coh:
                o = overall.setdefault(coh, {"paid": 0.0, "num": [0.0] * (kmax + 1), "segs": set(), "rows": 0})
                o["paid"] += paid
                o["rows"] += 1
                o["segs"].add(seg)
                for k in range(1, kmax + 1):
                    if vals[k - 1] is not None:
                        o["num"][k] += vals[k - 1]
                    else:
                        o["num"][k] = float("nan")

    segments: Dict[str, Any] = {}
    for seg, a in agg.items():
        # цепная кривая: шаг Mk-1 → Mk считается только по когортам, где известны оба месяца,
        # поэтому выпавшие ячейки и недожившие когорты не сдвигают форму кривой
        curve: List[Optional[float]] = [1.0]
        for k in range(1, kmax + 1):
            prev = curve[-1]
            curve.append(round(prev * a["num"][k] / a["den"][k], 4) if (prev is not None and a["den"][k]) else None)
        segments[seg] = {
            "trial_to_paid": round(a["paid_conv"] / a["trials"], 4) if a["trials"] else None,
            "curve": curve,
            "cohorts_per_month": [sum(1 for t in table if t["segment"] == seg and t["retention"][k] is not None)
                                  for k in range(kmax + 1)],
            **classify_shape(curve, flat_pp=flat_pp),
        }

    gaps: List[Dict[str, Any]] = []
    if len(segments) > 1:
        for k in range(1, kmax + 1):
            pts = {s: v["curve"][k] for s, v in segments.items() if v["curve"][k] is not None}
            if len(pts) < 2:
                continue
            hi = max(pts, key=pts.get)
            lo = min(pts, key=pts.get)
            g = round((pts[hi] - pts[lo]) * 100, 1)
            gaps.append({"month": f"M{k}", "max_segment": hi, "min_segment": lo, "gap_pp": g, "flag": g > gap_pp})

    missing_cohorts: List[Dict[str, str]] = []
    all_months = sorted({m for ms in seg_months.values() for m in ms})
    if all_months:
        for seg, ms in seg_months.items():
            for m in range(all_months[0], all_months[-1] + 1):
                if m not in ms:
                    missing_cohorts.append({"segment": seg, "cohort": _month_name(m)})

    all_segs = set(agg)
    by_cohort = []
    for coh in sorted(overall):
        o = overall[coh]
        ret = [1.0] + [None if (math.isnan(o["num"][k]) or not o["num"][k]) else round(o["num"][k] / o["paid"], 4)
                       for k in range(1, kmax + 1)]
        by_cohort.append({"cohort": coh, "paid": o["paid"], "retention": ret,
                          "complete": o["segs"] == all_segs})

    return {"months": kmax, "rows": table, "overall_by_cohort": by_cohort, "segments": segments, "segment_gap": gaps,
            "gap_threshold_pp": gap_pp,
            "max_gap": max(gaps, key=lambda g: g["gap_pp"]) if gaps else None,
            "holes": holes, "missing_cohorts": missing_cohorts}


def cmd_cohort(a: argparse.Namespace) -> Dict[str, Any]:
    path = Path(a.csv).expanduser()
    if not path.is_file():
        raise CalcError(f"нет файла {path}")
    rows, sha = _read_csv(path)
    inputs = {"csv_sha256": sha, "csv_name": path.name, "gap_pp": a.gap_pp, "flat_pp": a.flat_pp}
    return _envelope("cohort", inputs, cohort_analysis(rows, a.gap_pp, a.flat_pp))


def human_cohort(d: Dict[str, Any]) -> str:
    r = d["result"]
    k = r["months"]
    head = "сегмент      когорта   триал→пл.  " + "  ".join(f"{'M' + str(j):>6}" for j in range(k + 1))
    out = [f"Удержание по когортам ({d['inputs']['csv_name']}), доля от оплативших M0", head]
    for t in r["rows"]:
        cells = "  ".join(f"{fmt_pct(x, 0):>6}" for x in t["retention"])
        out.append(f"{t['segment'][:12]:<12} {t['cohort']:<9} {fmt_pct(t['trial_to_paid'], 0):>9}  {cells}")
    out.append("")
    out.append("По продукту в целом (все сегменты вместе; * — в когорте не все сегменты)")
    for t in r["overall_by_cohort"]:
        cells = "  ".join(f"{fmt_pct(x, 0):>6}" for x in t["retention"])
        mark = "" if t["complete"] else "*"
        out.append(f"{'все':<12} {t['cohort'] + mark:<9} {'':>9}  {cells}")
    out.append("")
    out.append("Средние по сегментам (цепная кривая: шаг Mk-1→Mk по когортам, где известны оба месяца)")
    for s, v in r["segments"].items():
        cells = "  ".join(f"{fmt_pct(x, 0):>6}" for x in v["curve"])
        out.append(f"{s[:12]:<12} {'среднее':<9} {fmt_pct(v['trial_to_paid'], 0):>9}  {cells}"
                   f"   форма: {v['shape']} ({v['reason']})")
    mg = r["max_gap"]
    if mg:
        flag = "больше" if mg["flag"] else "не больше"
        out.append("")
        out.append(f"Наибольший разрыв сегментов: {mg['month']} — {mg['max_segment']} против {mg['min_segment']}, "
                   f"{fmt_num(mg['gap_pp'], 1)} п.п. ({flag} порога {fmt_num(r['gap_threshold_pp'], 0)} п.п.)")
        flagged = [g["month"] for g in r["segment_gap"] if g["flag"]]
        if flagged:
            out.append(f"  разрыв > порога в месяцах: {', '.join(flagged)} — средняя по продукту смешивает разные аудитории")
    if r["holes"]:
        out.append("Пропуски внутри рядов (не учитывались): " +
                   ", ".join(f"{h['segment']} {h['cohort']} {h['month']}" for h in r["holes"]))
    if r["missing_cohorts"]:
        out.append("Нет строк когорт: " + ", ".join(f"{m['segment']} {m['cohort']}" for m in r["missing_cohorts"]))
    out.append("Пустые хвосты рядов — когорта ещё не дожила до этого месяца.")
    return "\n".join(out)


# ---------------------------------------------------------------- ltv

def ltv_calc(arpu: float, margin: float, churn: float, cac: Optional[float] = None) -> Dict[str, Any]:
    if arpu <= 0:
        raise CalcError("arpu > 0 (₽ в месяц)")
    _share("margin", margin, allow_one=True)
    _share("churn", churn, allow_one=True)
    life = 1 / churn
    ltv = arpu * margin / churn
    res: Dict[str, Any] = {"lifetime_months": round(life, 4), "gross_profit_month": round(arpu * margin, 4),
                           "ltv": round(ltv, 2)}
    if cac is not None:
        if cac <= 0:
            raise CalcError("cac > 0")
        res["ltv_cac"] = round(ltv / cac, 4)
        res["payback_months"] = round(cac / (arpu * margin), 4)
    return res


def cmd_ltv(a: argparse.Namespace) -> Dict[str, Any]:
    inputs = {"arpu": a.arpu, "margin": a.margin, "churn": a.churn, "cac": a.cac}
    return _envelope("ltv", inputs, ltv_calc(a.arpu, a.margin, a.churn, a.cac))


def human_ltv(d: Dict[str, Any]) -> str:
    i, r = d["inputs"], d["result"]
    lines = ["LTV = ARPU · маржа / месячный отток",
             f"  ARPU {fmt_num(i['arpu'])} ₽ · маржа {fmt_pct(i['margin'])} / отток {fmt_pct(i['churn'])} в месяц",
             f"  средний срок жизни {fmt_num(r['lifetime_months'], 1)} мес., валовая прибыль {fmt_num(r['gross_profit_month'])} ₽/мес.",
             f"  LTV = {fmt_num(r['ltv'])} ₽"]
    if "ltv_cac" in r:
        lines.append(f"  CAC {fmt_num(i['cac'])} ₽ → LTV/CAC = {fmt_num(r['ltv_cac'], 2)}, "
                     f"окупаемость {fmt_num(r['payback_months'], 1)} мес.")
    lines.append("  Модель постоянного оттока: при кривой «плато» реальный LTV выше, при «ведре» — ниже.")
    return "\n".join(lines)


# ---------------------------------------------------------------- schet

SCHET_FUNCS = {"min": min, "max": max, "round": round, "sum": sum, "abs": abs}
SCHET_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
             ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
             ast.Pow: lambda a, b: a ** b}
SCHET_UN = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}
SCHET_MAX_LEN = 2000
SCHET_MAX_POW = 1000


def _schet_num(text: str) -> float:
    t = text.strip().replace("\u00a0", "").replace(" ", "").replace("_", "")
    t = t.replace(",", ".") if t.count(",") == 1 and "." not in t else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        raise CalcError(f"«{text}» — не число (пример: --var plan=5000 или --var share=0,3)") from None
    if not math.isfinite(v):
        raise CalcError(f"«{text}» — не конечное число")
    return int(v) if v.is_integer() and "." not in t and "e" not in t.lower() else v


def parse_vars(items: Optional[List[str]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for it in items or []:
        if "=" not in it:
            raise CalcError(f"--var «{it}»: нужен вид имя=число")
        name, val = it.split("=", 1)
        name = name.strip()
        if not name.isidentifier() or name.startswith("_") or name in SCHET_FUNCS:
            raise CalcError(f"--var «{name}»: имя — буквы, цифры и «_», не с «_» и не имя функции")
        out[name] = _schet_num(val)
    return out


def _schet_eval(node: ast.AST, names: Dict[str, float]) -> Any:
    if isinstance(node, ast.Expression):
        return _schet_eval(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(f"в выражении только числа, а не {node.value!r}")
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise CalcError(f"«{node.id}» не задано: добавь --var {node.id}=<число>")
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in SCHET_BIN:
        a, b = _schet_eval(node.left, names), _schet_eval(node.right, names)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and b == 0:
            raise CalcError("деление на ноль: проверь знаменатель (какая из величин равна 0?)")
        if isinstance(node.op, ast.Pow) and abs(b) > SCHET_MAX_POW:
            raise CalcError(f"степень {b} слишком большая (не больше {SCHET_MAX_POW})")
        return SCHET_BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in SCHET_UN:
        return SCHET_UN[type(node.op)](_schet_eval(node.operand, names))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_schet_eval(e, names) for e in node.elts]
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            raise CalcError("обращение к атрибутам (через точку) запрещено")
        if not isinstance(node.func, ast.Name) or node.func.id not in SCHET_FUNCS:
            what = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise CalcError(f"функция «{what}» не разрешена: только {', '.join(SCHET_FUNCS)}")
        if node.keywords:
            raise CalcError("именованные аргументы функций не поддерживаются")
        args = [_schet_eval(a, names) for a in node.args]
        try:
            return SCHET_FUNCS[node.func.id](*args)
        except TypeError as e:
            raise CalcError(f"{node.func.id}(…): {e}") from None
    if isinstance(node, ast.Attribute):
        raise CalcError("обращение к атрибутам (через точку) запрещено")
    raise CalcError(f"в выражении запрещено: {type(node).__name__}")


def schet(expr: str, names: Optional[Dict[str, float]] = None) -> Any:
    """Безопасный счёт выражения: разбор через ast, ничего из Python, кроме арифметики и белого списка функций."""
    expr = (expr or "").strip()
    if not expr:
        raise CalcError("пустое выражение")
    if len(expr) > SCHET_MAX_LEN:
        raise CalcError(f"выражение длиннее {SCHET_MAX_LEN} символов — разбей на шаги")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"не разобрал выражение: {e.msg}") from None
    try:
        v = _schet_eval(tree, dict(names or {}))
    except (OverflowError, ZeroDivisionError) as e:
        raise CalcError(f"не посчитать: {e}") from None
    if isinstance(v, list):
        raise CalcError("результат — список, а нужно одно число: оберни в sum/min/max; дробь пиши через точку (1.5)")
    if isinstance(v, float) and not math.isfinite(v):
        raise CalcError("результат не конечное число")
    return v


def _substitute(expr: str, names: Dict[str, float]) -> str:
    out = expr
    for n in sorted(names, key=len, reverse=True):
        out = re.sub(rf"(?<![\w.]){re.escape(n)}(?![\w(])", _fmt_plain(names[n]), out)
    return out


def _fmt_plain(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def cmd_schet(a: argparse.Namespace) -> Dict[str, Any]:
    names = parse_vars(a.var)
    v = schet(a.expr, names)
    r = round(v, a.digits) if isinstance(v, float) and a.digits is not None else v
    inputs = {"expr": a.expr, "vars": names, "label": a.label or ""}
    result = {"value": r, "formula": a.expr, "substituted": _substitute(a.expr, names)}
    return _envelope("schet", inputs, result)


def human_schet(d: Dict[str, Any]) -> str:
    i, r = d["inputs"], d["result"]
    v = r["value"]
    shown = fmt_num(v, 4).rstrip("0").rstrip(",") if isinstance(v, float) else fmt_int(v)
    lines = [f"Счёт{': ' + i['label'] if i.get('label') else ''}",
             f"  формула:     {r['formula']}"]
    if i["vars"]:
        lines.append("  где:         " + ", ".join(f"{k} = {_fmt_plain(x)}" for k, x in i["vars"].items()))
        lines.append(f"  подстановка: {r['substituted']}")
    lines.append(f"  итог:        {shown}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

HUMAN = {"mde": human_mde, "srm": human_srm, "cohort": human_cohort, "ltv": human_ltv, "schet": human_schet}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="calc.py", description="Воспроизводимые расчёты стратега")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="вывод в JSON")

    m = sub.add_parser("mde", help="выборка на вариант и срок теста")
    m.add_argument("--base", type=float, required=True, help="базовая доля, 0.04 = 4%%")
    m.add_argument("--lift", type=float, required=True, help="эффект: 0.10 = +10%% отн. (или абс. при --lift-type abs)")
    m.add_argument("--lift-type", choices=("rel", "abs"), default="rel")
    m.add_argument("--alpha", type=float, default=0.05)
    m.add_argument("--power", type=float, default=0.8)
    m.add_argument("--daily", type=float, help="поток участников в день на весь тест")
    m.add_argument("--variants", type=int, default=2)
    common(m)
    m.set_defaults(func=cmd_mde)

    s = sub.add_parser("srm", help="проверка пропорции групп (χ²)")
    s.add_argument("--a", type=int, required=True)
    s.add_argument("--b", type=int, required=True)
    s.add_argument("--ratio", type=float, default=0.5, help="задуманная доля группы A")
    s.add_argument("--threshold", type=float, default=0.001, help="порог p для SRM")
    common(s)
    s.set_defaults(func=cmd_srm)

    c = sub.add_parser("cohort", help="удержание по когортам из CSV")
    c.add_argument("--csv", required=True, help="cohort,[segment,]trials,paid,m1..mK; строки с # пропускаются")
    c.add_argument("--gap-pp", type=float, default=20.0, help="порог разрыва сегментов, п.п.")
    c.add_argument("--flat-pp", type=float, default=2.0, help="порог «плато»: падение за шаг, п.п.")
    common(c)
    c.set_defaults(func=cmd_cohort)

    l_ = sub.add_parser("ltv", help="LTV и LTV/CAC")
    l_.add_argument("--arpu", type=float, required=True, help="₽ в месяц на платящего")
    l_.add_argument("--margin", type=float, required=True, help="валовая маржа, доля")
    l_.add_argument("--churn", type=float, required=True, help="месячный отток, доля")
    l_.add_argument("--cac", type=float)
    common(l_)
    l_.set_defaults(func=cmd_ltv)

    sc = sub.add_parser("schet", help="счёт по числам брифа: выражение, подстановка, итог")
    sc.add_argument("expr", help="выражение, например \"(plan - fact) / plan\"")
    sc.add_argument("--var", action="append", metavar="ИМЯ=ЧИСЛО", help="именованное число (можно несколько)")
    sc.add_argument("--label", help="что считаем, для вывода")
    sc.add_argument("--digits", type=int, default=6, help="знаков после запятой в итоге (по умолчанию 6)")
    common(sc)
    sc.set_defaults(func=cmd_schet)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        data = args.func(args)
    except (CalcError, ValueError, OSError) as e:
        msg = {"error": str(e), "command": args.cmd}
        if args.json:
            print(json.dumps(msg, ensure_ascii=False))
        else:
            print(f"Ошибка: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(HUMAN[args.cmd](data))
        print(f"plan_hash: {data['plan_hash']}  ({VERSION})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
