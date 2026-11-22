"""Строка статуса PolinaBulochka: цена первой — «этап N/M | ток | % нед. | ≈₽ | утв | долг | возр | отказ | ctx» (≤100).

Вход — JSON statusline на stdin (cost, context_window, rate_limits.seven_day, transcript_path).
Пишет state/cost.json и замер недельного лимита. Без проекта — короткая строка. Никогда не падает.
«бюджет N%» — ANSI-красным при budget_used_pct ≥ budget_threshold×100 (config/pricing.json).
Сегменты расширений: каждый `.claude/hooks/ext/statusline_*.py` (по алфавиту) получает тот же stdin и печатает
короткую строку (≤30 симв.); сегменты встают после «% недели» (перед ctx). Общая видимая длина ≤ 100:
при нехватке места сначала уходит хвост базового слоя после слота (ctx), затем «отказ»/«возр», затем сегменты
расширений (с конца), затем хвостовые части головы базового слоя.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_LEN = 100
LOW_PRIORITY = ("возр ", "отказ ")  # уступают место сегментам расширений после ctx
SEGMENT_MAX = 30
SEGMENT_TIMEOUT = 2
EXT_DIR = Path(os.environ.get("BULOCHKA_EXT_HOOKS_DIR") or Path(__file__).resolve().parent / "ext")  # env — для тестов
SEP = " | "
RED, RESET = "\033[31m", "\033[0m"
ANSI = re.compile(r"\033\[[0-9;]*m")
DEFAULT_STEPS = ["intake", "plan", "research", "situation", "options", "strategy",
                 "validation-plan", "review", "decide", "revise"]
DEFAULT_N = {"intake": 1, "plan": 2, "research": 3, "situation": 4, "options": 5, "strategy": 6,
             "validation-plan": 7, "review": 8, "decide": 10, "revise": 11}


def step_numbering():
    """(номер шага по id, всего шагов) — из project.py (срез «состояние»), иначе по таблице CLAUDE.md."""
    try:
        from tools.bulochka import project
        return dict(project.STEP_N), int(project.STEPS_TOTAL)
    except Exception:
        return DEFAULT_N, 11


def is_verified(c):
    if c.get("verified") is True:
        return True
    for k in ("verify_status", "quote_check", "verification"):
        if c.get(k) in ("pass", "ok", "verified"):
            return True
    return c.get("type") == "fact" and c.get("number_on_page") == "pass"


def counts(core, slug):
    from tools.bulochka.decisions import total_evidence_debt
    claims = [c for c in core.latest("claim", slug).values() if c.get("status", "active") == "active"]
    verified = sum(1 for c in claims if is_verified(c))
    debt = total_evidence_debt(slug)  # та же функция, что в `bulochka status`
    obj_open = sum(1 for r in core.latest("objection", slug).values() if r.get("status", "open") == "open")
    ref_open = sum(1 for x in core.latest("refusal", slug).values() if x.get("outcome", "pending") == "pending")
    return len(claims), verified, debt, obj_open, ref_open


def vlen(s):
    """Видимая длина без ANSI-кодов."""
    return len(ANSI.sub("", s))


def fit(parts):
    parts = [p for p in parts if p]
    line = SEP.join(parts)
    while vlen(line) > MAX_LEN and len(parts) > 1:
        parts = parts[:-1]
        line = SEP.join(parts)
    if vlen(line) > MAX_LEN:
        line = ANSI.sub("", line)[:MAX_LEN]
    return line


def fit_with_ext(parts, segs, slot):
    """Строка базового слоя parts со вставкой сегментов segs после parts[slot-1]. При нехватке места первым уходит хвост
    базового слоя после слота (ctx), затем сегменты расширений (с конца), затем хвост головы базового слоя."""
    parts = [p for p in parts if p]
    segs = [x for x in segs if x]
    slot = min(slot, len(parts))
    head, tail = parts[:slot], parts[slot:]
    minor = [p for p in head if ANSI.sub("", p).startswith(LOW_PRIORITY)]
    while segs:
        heads = [head] + [[p for p in head if p not in minor[:k]] for k in range(1, len(minor) + 1)]
        for h in heads:
            for t in range(len(tail), -1, -1):
                line = SEP.join(h + segs + tail[:t])
                if vlen(line) <= MAX_LEN:
                    return line
        segs = segs[:-1]
    return fit(parts)


def ext_segments(raw):
    """Строки из ext/statusline_*.py; сбой сегмента — пропуск, не падение."""
    out = []
    try:
        scripts = sorted(EXT_DIR.glob("statusline_*.py"))
    except OSError:
        return out
    for path in scripts:
        try:
            r = subprocess.run([sys.executable, str(path)], input=raw, capture_output=True, text=True,
                               timeout=SEGMENT_TIMEOUT, env=dict(os.environ))
            line = ANSI.sub("", (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else "")
            if r.returncode == 0 and line:
                out.append(line[:SEGMENT_MAX])
        except Exception:  # noqa: BLE001
            continue
    return out


def budget_threshold_pct():
    try:
        from tools.bulochka import cost
        return float(cost.pricing().get("budget_threshold") or 0.8) * 100
    except Exception:  # noqa: BLE001
        return 80.0


def week_part(week_pct, delta):
    """«+Δ% нед.» за прогон; пока замер один — текущий % недельного лимита."""
    if delta:
        return f"+{delta:g}% нед."
    if week_pct is not None:
        return f"{week_pct:g}% нед."
    return ""


def build(stdin, raw=""):
    ctx = (stdin.get("context_window") or {}).get("used_percentage")
    week = ((stdin.get("rate_limits") or {}).get("seven_day") or {}).get("used_percentage")
    ctx_part = f"ctx {ctx:g}%" if isinstance(ctx, (int, float)) else ""
    try:
        from tools.bulochka import core, cost
        slug = core.current_slug()
        if not slug or not (core.projects_dir() / slug).exists():
            return fit(["PolinaBulochka | сметы ещё нет", week_part(week, None), ctx_part])
    except Exception:
        return fit(["PolinaBulochka", ctx_part])
    try:
        d = cost.compute(slug, stdin=stdin, writer="statusline")
        nums, total = step_numbering()
        step = d.get("step")
        head = f"этап {nums[step]}/{total}" if step in nums else f"этап —/{total}"
        c, v, debt, obj, ref = counts(core, slug)
        week_s = week_part(week, d.get("weekly_limit_pct_delta"))
        parts = [head, f"{cost.fmt_tok(d.get('tokens'))} ток", week_s]  # цена — первой
        if d.get("budget_used_pct") is not None and d["budget_used_pct"] >= budget_threshold_pct():
            parts.insert(1, f"{RED}бюджет {d['budget_used_pct']}%{RESET}")
        slot = len([p for p in parts if p])  # «≈N ₽ подп.» и другие сегменты — сразу после доли недели
        rest = [f"утв {c} ({v}✓)", f"долг {debt}", f"возр {obj}", f"отказ {ref}"]
        return fit_with_ext(parts + rest + [ctx_part], ext_segments(raw), slot)
    except Exception as e:  # строка статуса не должна падать
        try:
            core.append_jsonl(core.state_dir(slug) / "incidents.jsonl",
                              {"ts": core.now_iso(), "hook": "statusline", "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
        return fit([f"PolinaBulochka | {slug}", week_part(week, None), ctx_part])


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
        stdin = json.loads(raw) if raw.strip() else {}
        if not isinstance(stdin, dict):
            stdin = {}
    except Exception:
        stdin = {}
    try:
        print(build(stdin, raw if isinstance(raw, str) else ""))
    except Exception:
        print("PolinaBulochka")
    return 0


if __name__ == "__main__":
    sys.exit(main())
