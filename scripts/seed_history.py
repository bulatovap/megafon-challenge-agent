#!/usr/bin/env python3
"""Засеять `knowledge/runs/history.jsonl` прошлыми прогонами по их папкам проектов (state/cost.json и реестры).

    python3 scripts/seed_history.py <папка проекта> --brief-id B01 [--run-id g1-b01]
        [--weekly-range 1..2 --weekly-source "замер человека"]

Токены по шагам, минуты, вопросы плана, источники — из файлов проекта. Δ% недели — из cost.json (замеры
statusline); если их нет, можно передать диапазон, измеренный человеком (`--weekly-range`), он хранится
отдельно от замеров и в смете показывается с пометкой. Повторный запуск с тем же run_id строку не дублирует.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.bulochka import core, cost, x_econ  # noqa: E402


def latest(path: Path):
    cur = {}
    for r in core.read_jsonl(path):
        if r.get("id"):
            cur[r["id"]] = r
    return cur


def row_from_project(pdir: Path, brief_id: str, run_id: str, weekly_range=None, weekly_source=None):
    st = pdir / "state"
    c = json.loads((st / "cost.json").read_text(encoding="utf-8"))
    by_step = x_econ.step_tokens(c)
    gates = latest(st / "gates.jsonl")
    secs = [g["review_seconds"] for g in gates.values() if isinstance(g.get("review_seconds"), (int, float))]
    claims = latest(st / "claims.jsonl")
    shown = set()
    for g in gates.values():
        if g.get("answer") not in (None, ""):
            shown.update(g.get("claims_shown") or [])
    vmin = sum(float(cl.get("verify_minutes") or 0) for cid, cl in claims.items() if cid in shown)
    plan = pdir / "cost_plan.md"
    q = len(set(re.findall(r"\bQ(\d{1,2})\b", plan.read_text(encoding="utf-8")))) if plan.exists() else None
    est = c.get("estimate") or {}
    tokens = int(c.get("tokens") or 0)
    row = {"run_id": run_id, "slug": pdir.name, "brief_id": brief_id, "ts": c.get("ts") or core.now_iso(),
           "tokens": tokens, "tokens_by_step": by_step, "steps": sorted(by_step),
           "minutes_human": c.get("minutes_human"), "review_seconds_median": statistics.median(secs) if secs else None,
           "verify_minutes": vmin, "questions": q or None, "sources": len(latest(st / "sources.jsonl")),
           "weekly_delta": c.get("weekly_limit_pct_delta"),
           "estimate": est or None, "estimate_tokens": est.get("tokens"),
           "error_pct": c.get("forecast_error_pct"), "seeded": True}
    if weekly_range:
        row["weekly_delta_range"] = weekly_range
        row["weekly_delta_source"] = weekly_source or "замер человека"
    return row


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("project", help="папка проекта (с state/cost.json)")
    p.add_argument("--brief-id", required=True)
    p.add_argument("--run-id")
    p.add_argument("--weekly-range", help="диапазон Δ%% недели, измеренный человеком: 1..2")
    p.add_argument("--weekly-source")
    a = p.parse_args(argv)
    pdir = Path(a.project).resolve()
    wr = None
    if a.weekly_range:
        lo, hi = (float(x.replace(",", ".")) for x in a.weekly_range.split(".."))
        wr = [lo, hi]
    row = row_from_project(pdir, a.brief_id, a.run_id or f"{pdir.name}@seed", wr, a.weekly_source)
    added = x_econ.append_history(row)
    print(f"{'добавлен' if added else 'уже есть'}: {row['run_id']} — {cost.fmt_tok(row['tokens'])} ток, "
          f"шагов {len(row['tokens_by_step'])}, вопросов {row['questions']}, источников {row['sources']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
