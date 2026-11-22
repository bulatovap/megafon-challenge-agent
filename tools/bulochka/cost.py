"""Экономика прогона: токены из транскрипта Claude Code, минуты продакта, доля недельного лимита подписки.

Источник правды по объёму — транскрипт сессии (`message.usage`), включая `subagents/*.jsonl` рядом с ним;
одно сообщение API пишется в транскрипт несколькими строками (по блоку контента) — дедуп по `message.id`.
Деньги — доля стоимости подписки (замечание 6 контракта), а не цена API:
    rub_equiv = plan_price_month × 12/52 × Δ% недельного лимита / 100 × fx_rate.
`cost.total_cost_usd` из statusline хранится только для сверки (`statusline_cost_usd`).

Заголовочное число «токены» = вход + выход + запись кэша. Чтение кэша показывается отдельно:
каждый запрос перечитывает весь контекст, и с ним число теряет смысл для бюджета шага.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import core

TOKEN_KEYS = ("in", "out", "cache_read", "cache_write")
PRE_STEP = "до проекта"
NO_STEP = "—"

DEFAULT_PRICING: Dict[str, Any] = {
    "plan": "Claude Max",
    "plan_price_month": 200,
    "currency": "USD",
    "plan_price_date": "2026-11-09",
    "fx_rate": 81,
    "fx_date": "2026-11-09",
    "step_budget_tokens": 400000,
    "step_budgets": {},
    "budget_threshold": 0.8,
    "estimate": {"tokens_per_step": 60000, "tokens_per_brief_char": 4, "steps_total": 11,
                 "minutes_per_gate": 3, "gates": 3},
}


# ---------- конфиг и окружение ----------

def pricing() -> Dict[str, Any]:
    cfg = dict(DEFAULT_PRICING)
    try:
        cfg.update(core.load_config("pricing.json", {}) or {})
    except (OSError, ValueError):
        pass
    return cfg


def week_price(cfg: Optional[Dict[str, Any]] = None) -> float:
    """Цена подписки за неделю в валюте тарифа: месяц × 12 / 52."""
    cfg = cfg or pricing()
    return float(cfg["plan_price_month"]) * 12.0 / 52.0


def money_from_pct(delta_pct: Optional[float], cfg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], Optional[float]]:
    """(usd, rub) — доля стоимости подписки за Δ% недельного лимита."""
    if delta_pct is None:
        return None, None
    cfg = cfg or pricing()
    usd = week_price(cfg) * float(delta_pct) / 100.0
    return round(usd, 2), round(usd * float(cfg["fx_rate"]), 0)


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def statusline_snapshot() -> Dict[str, Any]:
    """Последний JSON statusline, если его сохраняет пользовательский statusline (не обязателен)."""
    p = claude_home() / "statusline-last.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_transcript(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    hits = glob.glob(str(claude_home() / "projects" / "*" / f"{glob.escape(session_id)}.jsonl"))
    return hits[0] if hits else None


def transcript_files(path: str) -> List[Path]:
    """Транскрипт сессии + транскрипты субагентов в `<session>/subagents/*.jsonl`."""
    p = Path(path)
    files = [p] if p.exists() else []
    sub = p.with_suffix("") / "subagents"
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.jsonl")))
    return files


# ---------- разбор транскрипта ----------

def parse_ts(s: Any) -> Optional[float]:
    if not s or not isinstance(s, str):
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        # 2026-11-09T16:54:51.918Z — fromisoformat в 3.9 не всегда ест миллисекунды с Z
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(s2, fmt).timestamp()
            except ValueError:
                continue
    return None


def usage_tokens(u: Dict[str, Any]) -> Dict[str, int]:
    return {
        "in": int(u.get("input_tokens") or 0),
        "out": int(u.get("output_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
    }


def read_messages(paths: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """message.id → {ts, model, sidechain, tokens}. Повторы id (блоки одного ответа, копии) — одна запись."""
    out: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        try:
            f = open(str(path), encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for n, line in enumerate(f):
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                if m.get("model") == "<synthetic>":
                    continue
                mid = m.get("id") or d.get("requestId") or f"{path}:{n}"
                out[mid] = {
                    "ts": parse_ts(d.get("timestamp")),
                    "model": m.get("model"),
                    "sidechain": bool(d.get("isSidechain")),
                    "tokens": usage_tokens(m["usage"]),
                }
    return out


def zero() -> Dict[str, int]:
    return {k: 0 for k in TOKEN_KEYS}


def add(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    return {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in TOKEN_KEYS}


def headline(t: Dict[str, int]) -> int:
    """Заголовочные токены: вход + выход + запись кэша (без чтения кэша)."""
    return int(t.get("in", 0)) + int(t.get("out", 0)) + int(t.get("cache_write", 0))


def fmt_tok(n: Optional[float]) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"


# ---------- шаги по журналу ----------

def step_timeline(slug: Optional[str] = None) -> List[Tuple[float, str]]:
    """(ts, step) по всем записям журнала с шагом, по возрастанию времени."""
    try:
        recs = core.read_jsonl(core.entity_path("journal", slug))
    except core.BulochkaError:
        return []
    tl = []
    for r in recs:
        t = parse_ts(r.get("ts") or r.get("created_at"))
        if t is not None and r.get("step"):
            tl.append((t, str(r["step"])))
    tl.sort(key=lambda x: x[0])
    return tl


def step_at(tl: List[Tuple[float, str]], ts: Optional[float]) -> str:
    if not tl:
        return NO_STEP
    if ts is None:
        return tl[-1][1]
    i = bisect.bisect_right([t for t, _ in tl], ts) - 1
    return tl[i][1] if i >= 0 else PRE_STEP


def by_step(msgs: Dict[str, Dict[str, Any]], tl: List[Tuple[float, str]]) -> Dict[str, Dict[str, int]]:
    res: Dict[str, Dict[str, int]] = {}
    for m in msgs.values():
        s = step_at(tl, m.get("ts"))
        res[s] = add(res.get(s, zero()), m["tokens"])
    return res


def current_step(slug: Optional[str] = None) -> Optional[str]:
    tl = step_timeline(slug)
    return tl[-1][1] if tl else None


# ---------- недельный лимит ----------

def limits_path(slug: Optional[str] = None) -> Path:
    return core.state_dir(slug) / "limit_samples.jsonl"


def record_limit_sample(pct: Any, resets_at: Any, session_id: Optional[str] = None,
                        slug: Optional[str] = None) -> bool:
    """Дописать замер % недельного лимита, только если он изменился. True — записан."""
    if pct is None:
        return False
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return False
    samples = core.read_jsonl(limits_path(slug))
    last = samples[-1] if samples else None
    if last and last.get("pct") == pct and last.get("resets_at") == resets_at:
        return False
    core.append_jsonl(limits_path(slug), {"ts": core.now_iso(), "pct": pct, "resets_at": resets_at,
                                          "session_id": session_id})
    return True


def weekly_delta(samples: List[Dict[str, Any]], tl: Optional[List[Tuple[float, str]]] = None
                 ) -> Dict[str, Any]:
    """До/после и Δ п.п. недельного лимита. При сбросе недели (новый resets_at) отсчёт идёт от 0.

    Лимит общий на аккаунт: Δ включает параллельные сессии того же аккаунта.
    """
    samples = [s for s in samples if isinstance(s.get("pct"), (int, float))]
    if not samples:
        return {"before": None, "after": None, "delta": None, "by_step": {}, "resets_at": None}
    delta = 0.0
    steps: Dict[str, float] = {}
    for prev, cur in zip(samples, samples[1:]):
        if cur.get("resets_at") == prev.get("resets_at"):
            d = max(0.0, cur["pct"] - prev["pct"])
        else:
            d = cur["pct"]
        delta += d
        if tl is not None and d:
            s = step_at(tl, parse_ts(cur.get("ts")))
            steps[s] = steps.get(s, 0.0) + d
    return {"before": samples[0]["pct"], "after": samples[-1]["pct"], "delta": round(delta, 2),
            "by_step": {k: round(v, 2) for k, v in steps.items()}, "resets_at": samples[-1].get("resets_at")}


# ---------- минуты продакта ----------

def minutes_human(slug: Optional[str] = None) -> float:
    secs = 0
    try:
        for g in core.latest("gate", slug).values():
            if isinstance(g.get("review_seconds"), (int, float)):
                secs += g["review_seconds"]
    except core.BulochkaError:
        return 0.0
    return round(secs / 60.0, 1)


# ---------- сборка cost.json ----------

def cost_path(slug: Optional[str] = None) -> Path:
    return core.state_dir(slug) / "cost.json"


def load_cost(slug: Optional[str] = None) -> Dict[str, Any]:
    try:
        return json.loads(cost_path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError, core.BulochkaError):
        return {}


def _inside_root(path: Any) -> bool:
    if not path:
        return False
    try:
        Path(str(path)).resolve().relative_to(core.root())
        return True
    except (ValueError, OSError):
        return False


def known_sessions(slug: Optional[str], prev: Dict[str, Any], stdin: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """session_id → transcript_path: из cost.json, stdin statusline/хука, журнала, statusline-last.json."""
    sessions: Dict[str, str] = {k: v for k, v in (prev.get("sessions") or {}).items() if v}
    if stdin and stdin.get("session_id") and stdin.get("transcript_path"):
        sessions[stdin["session_id"]] = stdin["transcript_path"]
    try:
        for r in core.read_jsonl(core.entity_path("journal", slug)):
            sid = r.get("session_id")
            if sid and sid not in sessions:
                tp = find_transcript(sid)
                if tp:
                    sessions[sid] = tp
    except core.BulochkaError:
        pass
    snap = statusline_snapshot()
    ws = snap.get("workspace") or {}
    if snap.get("session_id") and snap.get("transcript_path") and (
            _inside_root(snap.get("cwd")) or _inside_root(ws.get("current_dir"))):
        sessions.setdefault(snap["session_id"], snap["transcript_path"])
    return sessions


def step_budget(step: Optional[str], prev: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    over = (prev.get("step_budgets") or {})
    if step and step in over:
        return int(over[step])
    if step and step in (cfg.get("step_budgets") or {}):
        return int(cfg["step_budgets"][step])
    return int(cfg.get("step_budget_tokens") or DEFAULT_PRICING["step_budget_tokens"])


def compute(slug: Optional[str] = None, stdin: Optional[Dict[str, Any]] = None,
            transcripts: Optional[List[str]] = None, write: bool = True, writer: str = "cli") -> Dict[str, Any]:
    """Пересчитать экономику проекта и (по умолчанию) записать state/cost.json."""
    cfg = pricing()
    prev = load_cost(slug)
    sessions = known_sessions(slug, prev, stdin)
    paths: List[Path] = []
    for tp in list(sessions.values()) + list(transcripts or []):
        for f in transcript_files(tp):
            if f not in paths:
                paths.append(f)
    msgs = read_messages(paths)
    tl = step_timeline(slug)
    steps = by_step(msgs, tl)
    pre = steps.pop(PRE_STEP, zero())
    total = zero()
    for t in steps.values():
        total = add(total, t)

    if stdin:
        rl = ((stdin.get("rate_limits") or {}).get("seven_day") or {})
        if rl.get("used_percentage") is not None:
            record_limit_sample(rl.get("used_percentage"), rl.get("resets_at"), stdin.get("session_id"), slug)
    week = weekly_delta(core.read_jsonl(limits_path(slug)), tl)
    usd_total, rub_total = money_from_pct(week["delta"], cfg)

    step = tl[-1][1] if tl else None
    t_step = steps.get(step, zero()) if step else total
    budget = step_budget(step, prev, cfg)
    step_pct = week["by_step"].get(step) if step else week["delta"]
    usd_step, rub_step = money_from_pct(step_pct, cfg)

    last = max(msgs.values(), key=lambda m: m.get("ts") or 0) if msgs else {}
    ctx = None
    sl_cost = prev.get("statusline_cost_usd")
    if stdin:
        ctx = (stdin.get("context_window") or {}).get("used_percentage")
        sl_cost = (stdin.get("cost") or {}).get("total_cost_usd", sl_cost)
    estimate = prev.get("estimate")
    fe = None
    if estimate and estimate.get("tokens"):
        fe = round((headline(total) - estimate["tokens"]) / float(estimate["tokens"]) * 100.0, 1)

    data: Dict[str, Any] = {
        "ts": core.now_iso(),
        "writer": writer,
        "session_id": (stdin or {}).get("session_id") or prev.get("session_id"),
        "model": last.get("model") or prev.get("model"),
        "step": step,
        "tokens": headline(total),
        "tokens_total": total,
        "tokens_step": t_step,
        "tokens_by_step": steps,
        "tokens_pre_project": pre,
        "messages_counted": len(msgs),
        "step_budget_tokens": budget,
        "budget_used_pct": round(headline(t_step) * 100.0 / budget) if (budget and step) else None,
        "weekly_limit_pct_before": week["before"],
        "weekly_limit_pct_after": week["after"],
        "weekly_limit_pct_delta": week["delta"],
        "weekly_limit_pct_by_step": week["by_step"],
        "weekly_resets_at": week["resets_at"],
        "plan": cfg.get("plan"),
        "plan_price_month": cfg.get("plan_price_month"),
        "plan_currency": cfg.get("currency"),
        "plan_price_date": cfg.get("plan_price_date"),
        "price_date": cfg.get("plan_price_date"),
        "fx_rate": cfg.get("fx_rate"),
        "fx_date": cfg.get("fx_date"),
        "money_basis": "доля стоимости подписки: цена в месяц × 12/52 × Δ% недельного лимита",
        "usd_equiv_total": usd_total,
        "rub_equiv_total": rub_total,
        "usd_equiv_step": usd_step,
        "rub_equiv_step": rub_step,
        "statusline_cost_usd": sl_cost,
        "context_used_pct": ctx if ctx is not None else prev.get("context_used_pct"),
        "minutes_human": minutes_human(slug),
        "estimate": estimate,
        "forecast_error_pct": fe,
        "step_budgets": prev.get("step_budgets") or {},
        "sessions": sessions,
    }
    if write:
        core.atomic_write_json(cost_path(slug), data)
    return data


def make_estimate(brief_text: str, slug: Optional[str] = None, prev: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    cfg = pricing()
    e = cfg.get("estimate") or DEFAULT_PRICING["estimate"]
    tokens = int(e["steps_total"]) * int(e["tokens_per_step"]) + len(brief_text) * int(e["tokens_per_brief_char"])
    est: Dict[str, Any] = {"tokens": tokens, "minutes_human": int(e["gates"]) * float(e["minutes_per_gate"]),
                           "made_at": core.now_iso(), "brief_chars": len(brief_text)}
    prev = prev or {}
    # Если прошлые прогоны дали связь «токены ↔ % недели» — прогноз доли лимита и рублей
    if prev.get("tokens") and prev.get("weekly_limit_pct_delta"):
        pct = tokens * float(prev["weekly_limit_pct_delta"]) / float(prev["tokens"])
        est["weekly_limit_pct"] = round(pct, 1)
        est["usd_equiv"], est["rub_equiv"] = money_from_pct(pct, cfg)
    return est


# ---------- CLI ----------

def human_summary(d: Dict[str, Any], show_step: Optional[str] = None) -> str:
    t = d.get("tokens_total") or zero()
    lines = [
        f"Токены: {fmt_tok(d.get('tokens'))} (вход {fmt_tok(t['in'])}, выход {fmt_tok(t['out'])}, "
        f"запись кэша {fmt_tok(t['cache_write'])}; чтение кэша {fmt_tok(t['cache_read'])} — отдельно)",
        f"Минуты продакта: {d.get('minutes_human', 0)}",
    ]
    if d.get("weekly_limit_pct_delta") is not None:
        lines.append(f"Недельный лимит: {d['weekly_limit_pct_before']:g}% → {d['weekly_limit_pct_after']:g}% "
                     f"(+{d['weekly_limit_pct_delta']:g} п.п.)")
        lines.append(f"Из стоимости подписки: ≈{d['rub_equiv_total']:.0f} ₽ (≈${d['usd_equiv_total']:.2f}) — "
                     f"${d['plan_price_month']}/мес × 12/52 × Δ% × {d['fx_rate']} ₽/$, на {d['plan_price_date']}")
    else:
        lines.append("Недельный лимит: замеров ещё нет (их пишет statusline)")
    if show_step:
        step = d.get("step") if show_step == "current" else show_step
        ts = (d.get("tokens_by_step") or {}).get(step, zero()) if step else d.get("tokens_step") or zero()
        budget = d.get("step_budget_tokens")
        lines.append(f"Шаг {step or '—'}: {fmt_tok(headline(ts))} ток из бюджета {fmt_tok(budget)}"
                     + (f" ({round(headline(ts) * 100 / budget)}%)" if budget else ""))
    if d.get("estimate"):
        e = d["estimate"]
        lines.append(f"Смета: {fmt_tok(e.get('tokens'))} ток, {e.get('minutes_human')} мин продакта"
                     + (f"; ошибка прогноза {d['forecast_error_pct']:+g}%" if d.get("forecast_error_pct") is not None
                        else ""))
    return "\n".join(lines)


def cmd_cost(args) -> int:
    slug = core.current_slug()
    if not slug:
        raise core.BulochkaError("Нет текущего проекта: создайте его командой `bulochka new <slug> --brief <file>`.")
    core.project_dir(slug)
    if args.estimate:
        if not args.brief:
            raise core.BulochkaError("Для сметы нужен бриф: `bulochka cost --estimate --brief <file>`.")
        bp = Path(args.brief)
        if not bp.is_absolute():
            bp = Path.cwd() / bp
        if not bp.exists():
            raise core.BulochkaError(f"Бриф не найден: {bp}")
        prev = load_cost(slug)
        est = make_estimate(bp.read_text(encoding="utf-8"), slug, prev)
        prev["estimate"] = est
        core.atomic_write_json(cost_path(slug), prev)
        human = (f"Смета прогона: ~{fmt_tok(est['tokens'])} токенов, ~{est['minutes_human']:g} мин продакта"
                 + (f", ~{est['weekly_limit_pct']}% недельного лимита (≈{est['rub_equiv']:.0f} ₽ из стоимости подписки)"
                    if est.get("weekly_limit_pct") is not None else ""))
        return core.emit("cost", {"estimate": est}, human=human, as_json=args.json)
    if args.set_budget is not None:
        prev = load_cost(slug)
        step = (args.step if args.step not in (None, "current") else None) or current_step(slug) or NO_STEP
        prev.setdefault("step_budgets", {})[step] = int(args.set_budget)
        core.atomic_write_json(cost_path(slug), prev)
    transcripts = [args.transcript] if args.transcript else None
    d = compute(slug, transcripts=transcripts, writer="cli")
    data = {k: d.get(k) for k in ("tokens", "tokens_total", "tokens_step", "tokens_by_step", "step",
                                  "step_budget_tokens", "budget_used_pct", "minutes_human",
                                  "weekly_limit_pct_before", "weekly_limit_pct_after", "weekly_limit_pct_delta",
                                  "usd_equiv", "rub_equiv_total", "usd_equiv_total", "price_date", "fx_rate",
                                  "estimate", "forecast_error_pct", "messages_counted", "statusline_cost_usd")}
    data["usd_equiv"] = d.get("usd_equiv_total")
    data["rub_equiv"] = d.get("rub_equiv_total")
    return core.emit("cost", data, human=human_summary(d, args.step), as_json=args.json)


def register(sub) -> None:
    p = sub.add_parser("cost", help="токены, минуты продакта, доля недельного лимита; смета прогона")
    p.add_argument("--estimate", action="store_true", help="смета прогона по брифу")
    p.add_argument("--brief", help="файл брифа для сметы")
    p.add_argument("--step", nargs="?", const="current", default=None, help="показать шаг (по умолчанию текущий)")
    p.add_argument("--set-budget", type=int, dest="set_budget", help="бюджет шага в токенах")
    p.add_argument("--transcript", help="путь к транскрипту сессии (если не найден автоматически)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="машиночитаемый ответ")
    p.set_defaults(func=cmd_cost)
