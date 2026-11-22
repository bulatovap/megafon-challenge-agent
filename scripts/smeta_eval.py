#!/usr/bin/env python3
"""Точность сметы: leave-one-out по истории прогонов → `projects/<slug>/report/smeta_eval.md` текущего проекта
(без текущего проекта — `report/smeta_eval.md` в корне).

Для каждого прогона смета строится по остальным (той же функцией, что `bulochka smeta`) на его же шагах,
с его числом вопросов плана и режимом сбора (поиск / только бриф), и сравнивается с фактом. Цель G2 — |ошибка| ≤ 30% на каждом. Exit 0 — цель выполнена, 1 — нет или мало прогонов.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.bulochka import core, cost, x_econ, x_smeta  # noqa: E402

TARGET = 30.0
MIN_RUNS = 3


def evaluate(rows):
    out = []
    for r in rows:
        others = [x for x in rows if x["run_id"] != r["run_id"]]
        if not others or not r.get("tokens"):
            continue
        est = x_smeta.estimate("", rows=others, steps=r.get("steps") or None, collect=x_smeta.row_collect(r),
                               questions=r.get("questions"))
        err = (r["tokens"] - est["tokens"]) / float(est["tokens"]) * 100.0
        out.append({"run_id": r["run_id"], "brief_id": r.get("brief_id"), "collect": est["collect"],
                    "estimate": est["tokens"],
                    "fact": r["tokens"], "error_pct": round(err, 1)})
    return out


def main():
    rows = x_econ.history()
    res = evaluate(rows)
    lines = ["# Точность сметы (leave-one-out)", "",
             f"Прогонов в истории: {len(rows)}. Цель: |ошибка| ≤ {TARGET:g}% на каждом.", ""]
    if len(rows) < MIN_RUNS:
        lines.append(f"Мало прогонов для оценки: нужно ≥{MIN_RUNS}. Добавьте прогоны: `bulochka smeta close`.")
    if res:
        lines += ["| Прогон | Бриф | Сбор | Смета, ток | Факт, ток | Ошибка |", "|---|---|---|---|---|---|"]
        lines += [f"| {x['run_id']} | {x['brief_id'] or '—'} | {'поиск' if x['collect'] == 'web' else 'только бриф'} | "
                  f"{cost.fmt_tok(x['estimate'])} | "
                  f"{cost.fmt_tok(x['fact'])} | {x['error_pct']:+g}% |" for x in res]
    ok = len(rows) >= MIN_RUNS and res and all(abs(x["error_pct"]) <= TARGET for x in res)
    lines += ["", f"Итог: {'цель выполнена' if ok else 'цель не выполнена'}.", ""]
    try:
        out = core.project_dir() / "report" / "smeta_eval.md"
    except core.BulochkaError:
        out = core.root() / "report" / "smeta_eval.md"
    core.atomic_write(out, "\n".join(lines))
    print("\n".join(lines))
    print(f"Записано: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
