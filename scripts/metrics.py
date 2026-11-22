"""Все числа для README, PDF и видео — только отсюда, из файлов репозитория и проекта.

    python3 scripts/metrics.py [--project <slug>] [--md]

Без --project берёт projects/CURRENT; без проекта считает только репозиторий.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bulochka import core, cost  # noqa: E402


def repo_metrics() -> dict:
    r = core.root()
    tests = 0
    for p in (r / "tests").glob("test_*.py"):
        tests += len(re.findall(r"^\s+def test_", p.read_text(encoding="utf-8"), re.M))
    claude_md = r / "CLAUDE.md"
    cli_cmds = []
    try:
        from tools.bulochka import cli
        parser = cli.build_parser()
        for a in parser._actions:  # noqa: SLF001 — читаем зарегистрированные команды
            if getattr(a, "choices", None) and isinstance(a.choices, dict):
                cli_cmds = sorted(a.choices)
    except Exception:
        pass
    py_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in (r / "tools" / "bulochka").glob("*.py"))
    return {
        "skills": len(list((r / ".claude" / "skills").glob("*/SKILL.md"))),
        "subagents": len(list((r / ".claude" / "agents").glob("*.md"))),
        "hooks": len([p for p in (r / ".claude" / "hooks").glob("*.py") if not p.name.startswith("_")]),
        "references": len(list((r / "knowledge" / "references").glob("*.md"))),
        "briefs": len(list((r / "knowledge" / "briefs").glob("*.md"))),
        "templates": len(list((r / "knowledge" / "templates").glob("*.md"))),
        "claude_md_lines": len(claude_md.read_text(encoding="utf-8").splitlines()) if claude_md.exists() else 0,
        "cli_commands": len(cli_cmds),
        "cli_command_names": cli_cmds,
        "cli_python_lines": py_lines,
        "tests": tests,
    }


def number_check(slug: str) -> dict:
    """state/number_check.json (отчёт «цифра найдена на странице», если его пишет расширение):
    found, total, became_hypotheses и доля найденных. Нет файла или он битый — пусто."""
    p = core.state_dir(slug) / "number_check.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    found, total = d.get("found"), d.get("total")
    became = d.get("became_hypotheses")
    if isinstance(became, list):
        became = len(became)
    out = {"numbers_found_on_page": found, "numbers_total": total, "numbers_became_hypotheses": became}
    if isinstance(found, (int, float)) and isinstance(total, (int, float)) and total:
        out["numbers_found_pct"] = round(found * 100.0 / total, 1)
    return {k: v for k, v in out.items() if v is not None}


def project_metrics(slug: str) -> dict:
    claims = list(core.latest("claim", slug).values())
    active = [c for c in claims if c.get("status", "active") == "active"]
    objections = list(core.latest("objection", slug).values())
    refusals = list(core.latest("refusal", slug).values())
    gates = list(core.latest("gate", slug).values())
    sources = list(core.latest("source", slug).values())
    options = list(core.latest("option", slug).values())
    decisions = list(core.latest("decision", slug).values())
    journal = core.read_jsonl(core.entity_path("journal", slug))
    c = cost.load_cost(slug)
    out = {
        "slug": slug,
        "sources_active": sum(1 for s in sources if s.get("status", "active") == "active"),
        "sources_excluded": sum(1 for s in sources if s.get("status") == "excluded"),
        "claims_active": len(active),
        "claims_by_type": dict(Counter(x.get("type") for x in active)),
        "claims_number_on_page": dict(Counter(x.get("number_on_page") for x in active if x.get("number_on_page"))),
        "objections": len(objections),
        "objections_open": sum(1 for r in objections if r.get("status", "open") == "open"),
        "options": len(options),
        "options_by_kind": dict(Counter(o.get("kind") for o in options)),
        "decisions": len(decisions),
        "decisions_by_status": dict(Counter(d.get("status") for d in decisions)),
        "refusals": len(refusals),
        "refusals_by_tag": dict(Counter(x.get("tag") for x in refusals)),
        "gates": len(gates),
        "gates_answered": sum(1 for g in gates if g.get("answer") is not None),
        "minutes_human": cost.minutes_human(slug),
        "journal_records": len(journal),
        "journal_by_status": dict(Counter(j.get("status") for j in journal)),
        "tokens": c.get("tokens"),
        "tokens_total": c.get("tokens_total"),
        "tokens_by_step": {k: cost.headline(v) for k, v in (c.get("tokens_by_step") or {}).items()},
        "weekly_limit_pct_before": c.get("weekly_limit_pct_before"),
        "weekly_limit_pct_after": c.get("weekly_limit_pct_after"),
        "weekly_limit_pct_delta": c.get("weekly_limit_pct_delta"),
        "rub_equiv_total": c.get("rub_equiv_total"),
        "usd_equiv_total": c.get("usd_equiv_total"),
        "money_basis": c.get("money_basis"),
        "plan_price_date": c.get("plan_price_date"),
        "fx_rate": c.get("fx_rate"),
        "estimate_tokens": (c.get("estimate") or {}).get("tokens"),
        "forecast_error_pct": c.get("forecast_error_pct"),
    }
    out.update(number_check(slug))
    return out


def collect(slug=None) -> dict:
    out = {"generated_at": core.now_iso(), "repo": repo_metrics()}
    slug = slug or core.current_slug()
    if slug and (core.projects_dir() / slug).exists():
        out["project"] = project_metrics(slug)
    return out


def to_md(m: dict) -> str:
    lines = [f"# Метрики ({m['generated_at']})", "", "| Показатель | Значение |", "|---|---|"]
    for sect in ("repo", "project"):
        for k, v in (m.get(sect) or {}).items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            lines.append(f"| {sect}.{k} | {v} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="числа для README/PDF из файлов")
    ap.add_argument("--project")
    ap.add_argument("--md", action="store_true")
    a = ap.parse_args(argv)
    m = collect(a.project)
    print(to_md(m) if a.md else json.dumps(m, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
