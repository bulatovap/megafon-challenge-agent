"""Проект и его состояние: new, status [--for-hook], next, pause, resume.

Состояние выводится из журнала (step.run committed = шаг done) и реестра ворот (gates.jsonl).
`state/state.json` и `RUN_STATUS.md` — кэш для людей и statusline, пересобирается после каждого изменения.
"""
from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import core, ext

# Шаги workflow (таблица «Шаги и ворота» в CLAUDE.md): (id = имя skill, номер в 11-шаговой схеме, название).
# Шаг review занимает номера 8–9 (механическая проверка + независимое чтение) — один skill.
STEPS = [
    ("intake", 1, "Постановка задачи"),
    ("plan", 2, "Switch points и план вопросов"),
    ("research", 3, "Сбор материалов"),
    ("situation", 4, "Картина ситуации"),
    ("options", 5, "Варианты и возражения"),
    ("strategy", 6, "Стратегия"),
    ("validation-plan", 7, "План проверки"),
    ("review", 8, "Проверка и независимое чтение"),
    ("decide", 10, "Решение"),
    ("revise", 11, "Сопровождение и пересмотр"),
]
STEPS_TOTAL = 11
STEP_IDS = [s[0] for s in STEPS]
STEP_TITLE = {s[0]: s[2] for s in STEPS}
STEP_SKILL = {'intake': 'order', 'plan': 'cost-plan', 'research': 'sourcing', 'situation': 'baseline', 'options': 'alternatives', 'strategy': 'playbook', 'validation-plan': 'pilot-design', 'review': 'controls', 'decide': 'approval', 'revise': 'reforecast'}
STEP_N = {s[0]: s[1] for s in STEPS}

# 3 ворот человека: вид → (шаг, на котором задаются; шаг, который без ответа не начинается; название)
GATES = {
    "brief_approval": ("plan", "research", "утверждение брифа и switch points"),
    "choose_option": ("options", "strategy", "выбор варианта"),
    "approve_decision": ("decide", "revise", "утверждение решения"),
}
GATE_BEFORE = {v[1]: k for k, v in GATES.items()}


TTL_BY_KIND = {"official": 365, "media": 90, "review": 30, "dataset": 180, "research": 365, "company": 365}
TTL_DEFAULT = 180

# Разделы квитанции остановки (STOP_RECEIPT.md, хук Stop) от расширений: fn(slug) -> str (markdown, обычно
# с заголовком «## …»). Модули x_*.py добавляют функции при импорте: project.RECEIPT_SECTIONS.append(fn).
RECEIPT_SECTIONS: List[Callable[[str], str]] = []
# Разделы RESUME.md (`bulochka pause`, конец сессии) от расширений: fn(slug) -> str, перед «Следующее действие».
RESUME_SECTIONS: List[Callable[[str], str]] = []

FOR_HOOK_MAX_CHARS = 4500  # ≈ ≤2500 токенов даже для кириллицы
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------- вычисление состояния ----------

def _gates(slug: Optional[str]) -> Dict[str, Dict[str, Any]]:
    return core.latest("gate", slug)


def _answered(g: Dict[str, Any]) -> bool:
    return g.get("answer") not in (None, "")


def step_statuses(slug: Optional[str]) -> Dict[str, Dict[str, Any]]:
    from . import journal
    out: Dict[str, Dict[str, Any]] = {s: {"status": "pending"} for s in STEP_IDS}
    rank = {"pending": 0, "failed": 1, "uncertain": 2, "in_progress": 3, "done": 4}
    for r in journal.by_key(slug).values():
        if r.get("operation") != "step.run" or r.get("step") not in out:
            continue
        st = {"committed": "done", "started": "in_progress", "uncertain": "uncertain",
              "failed": "failed"}.get(r.get("status"), "pending")
        cur = out[r["step"]]
        if rank[st] > rank[cur["status"]]:
            out[r["step"]] = {"status": st, "journal_id": r.get("id"), "output": r.get("output_path"),
                              "ts": r.get("ts")}
    return out


def _is_verified(c: Dict[str, Any]) -> bool:
    if c.get("verified") is True or c.get("quote_verified") is True:
        return True
    for f in ("verify_status", "verification", "verified_status"):
        if str(c.get(f, "")).lower() in ("pass", "ok", "verified"):
            return True
    if c.get("number_on_page") == "fail":
        return False
    if c.get("type") == "fact" and c.get("quote_offset"):
        return True  # цитата найдена на странице источника при claim add
    return c.get("number_on_page") == "pass"


def _stale_sources(slug: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    today = datetime.now(timezone.utc).date()
    for s in core.latest("source", slug).values():
        if s.get("status", "active") != "active" or not s.get("published"):
            continue
        try:
            pub = date.fromisoformat(str(s["published"])[:10])
        except ValueError:
            continue
        ttl = int(s.get("ttl_days") or TTL_BY_KIND.get(s.get("kind", ""), TTL_DEFAULT))
        age = (today - pub).days
        if age > ttl:
            out.append({"id": s["id"], "age_days": age, "ttl_days": ttl, "kind": "source"})
    return sorted(out, key=lambda x: x["id"])


def _stale_premises(slug: Optional[str], claims: Dict[str, Dict[str, Any]],
                    decisions: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for d in decisions.values():
        if d.get("status") in ("superseded", "invalidated"):
            continue
        for p in d.get("premises") or []:
            cid = p.get("claim_id") if isinstance(p, dict) else p
            c = claims.get(cid or "")
            if c and c.get("status") in ("superseded", "invalidated"):
                out.append({"id": cid, "kind": "premise", "of": d["id"], "status": c.get("status")})
    return out


def _cost(slug: Optional[str]) -> Dict[str, Any]:
    p = core.state_dir(slug) / "cost.json"
    if not p.exists():
        return {}
    try:
        import json
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _tokens_total(cost: Dict[str, Any]) -> Optional[int]:
    """Как в statusline: cost.json["tokens"] (вход + выход + запись кэша). Запасной путь — tokens_total без cache_read."""
    if isinstance(cost.get("tokens"), (int, float)):
        return int(cost["tokens"])
    t = cost.get("tokens_total")
    if isinstance(t, dict):
        return int(sum(v for k, v in t.items() if isinstance(v, (int, float)) and k != "cache_read"))
    if isinstance(t, (int, float)):
        return int(t)
    return None


def load_state_file(slug: Optional[str]) -> Dict[str, Any]:
    import json
    p = core.state_dir(slug) / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def compute(slug: Optional[str] = None) -> Dict[str, Any]:
    """Полная сводка состояния проекта (без записи)."""
    from . import journal
    slug = slug or core.current_slug()
    core.project_dir(slug)
    steps = step_statuses(slug)
    gates = _gates(slug)

    answered_kinds = {g.get("kind") for g in gates.values() if _answered(g)}
    open_gates = [{"id": g["id"], "kind": g.get("kind"), "question": g.get("question"), "step": g.get("step"),
                   "options": g.get("options")} for g in gates.values() if not _answered(g)]

    current = None
    blocked_by = None
    for sid in STEP_IDS:
        if steps[sid]["status"] == "done":
            continue
        gk = GATE_BEFORE.get(sid)
        if gk and gk not in answered_kinds:
            blocked_by = gk
        current = sid
        break
    waiting_gate = None
    if blocked_by:
        for g in open_gates:
            if g["kind"] == blocked_by:
                waiting_gate = g["id"]
                break

    claims = core.latest("claim", slug)
    active_claims = [c for c in claims.values() if c.get("status", "active") == "active"]
    objections = core.latest("objection", slug)
    refusals = core.latest("refusal", slug)
    options = core.latest("option", slug)
    decisions = core.latest("decision", slug)

    from .decisions import total_evidence_debt
    debt = total_evidence_debt(slug)

    cost = _cost(slug)
    uncertain = journal.uncertain_list(slug)
    stale = _stale_sources(slug) + _stale_premises(slug, claims, decisions)
    persistent = load_state_file(slug)

    done = [s for s in STEP_IDS if steps[s]["status"] == "done"]
    frozen = no_web_flag(slug).exists()

    data: Dict[str, Any] = {
        "slug": slug,
        "step": current,
        "step_title": STEP_TITLE.get(current or "", "все шаги пройдены"),
        "step_n": STEP_N[current] if current else STEPS_TOTAL,
        "steps_total": STEPS_TOTAL,
        "step_status": steps[current]["status"] if current else "done",
        "steps": steps,
        "done_steps": done,
        "blocked_by_gate": blocked_by,
        "waiting_gate": waiting_gate,
        "open_gates": open_gates,
        "counts": {
            "claims": len(active_claims),
            "verified": sum(1 for c in active_claims if _is_verified(c)),
            "objections_open": sum(1 for r in objections.values() if r.get("status", "open") == "open"),
            "refusals": len(refusals),
            "refusals_pending": sum(1 for x in refusals.values() if x.get("outcome", "pending") == "pending"),
            "evidence_debt": debt,
            "sources": sum(1 for s in core.latest("source", slug).values() if s.get("status", "active") == "active"),
            "options": len(options),
            "decisions": len(decisions),
        },
        "decisions": [{"id": d["id"], "status": d.get("status"), "question": d.get("question"),
                       "chosen_option": d.get("chosen_option"), "human_gate_id": d.get("human_gate_id")}
                      for d in decisions.values()],
        "answered_gates": [{"id": g["id"], "kind": g.get("kind"), "question": g.get("question"),
                            "answer": g.get("answer")} for g in gates.values() if _answered(g)],
        "stale": stale,
        "uncertain": uncertain,
        "sources_frozen": frozen,
        "tokens": _tokens_total(cost),
        "usd_equiv": cost.get("usd_equiv_total"),
        "rub_equiv": cost.get("rub_equiv_total"),
        "weekly_limit_pct": cost.get("weekly_limit_pct_after", cost.get("weekly_limit_pct")),
        "created_at": persistent.get("created_at"),
        "brief": persistent.get("brief", "order.md"),
    }
    data["next"] = next_action(data)
    return data


def next_action(st: Dict[str, Any]) -> Dict[str, Any]:
    """{step, skill, why} — что делать дальше."""
    pre = ""
    if st.get("uncertain"):
        ids = ", ".join(u["id"] for u in st["uncertain"][:5])
        pre = f"Сначала: незавершённые операции {ids} — покажите человеку, решает он (journal resolve). "
    cur = st.get("step")
    if cur is None:
        return {"step": None, "skill": "reforecast",
                "why": pre + "Все шаги пройдены. Пересмотр — при новых данных: bulochka decision due."}
    gk = st.get("blocked_by_gate")
    if gk:
        title = GATES[gk][2]
        if st.get("waiting_gate"):
            g = st["waiting_gate"]
            return {"step": cur, "skill": None,
                    "why": pre + f"Ждём ответа человека на {g} ({title}). Ответ записать: bulochka answer {g} --answer «…»."}
        ask_step = GATES[gk][0]
        return {"step": ask_step, "skill": STEP_SKILL[ask_step],
                "why": pre + f"Нужны ворота «{title}»: bulochka ask --kind {gk} --question … --options … "
                             f"--why … --default …"}
    status = st.get("step_status")
    verb = {"in_progress": "Продолжить", "uncertain": "Шаг прерван — решение за человеком, затем",
            "failed": "Повторить"}.get(status, "Начать")
    return {"step": cur, "skill": STEP_SKILL[cur],
            "why": pre + f"{verb} шаг {st['step_n']}/{st['steps_total']} «{STEP_TITLE[cur]}»: /{STEP_SKILL[cur]}."}


# ---------- тексты ----------

def _short(s: Any, n: int = 90) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def progress_line(st: Dict[str, Any]) -> str:
    line = f"шаг {st['step_n']}/{st['steps_total']} · {st['step_title']}"
    if st.get("waiting_gate"):
        line += f" · ждёт {st['waiting_gate']}"
    elif st.get("blocked_by_gate"):
        line += f" · нужны ворота {st['blocked_by_gate']}"
    return line


def for_hook_text(st: Dict[str, Any], *, max_chars: int = FOR_HOOK_MAX_CHARS) -> str:
    """Сводка для additionalContext: шаг N/M, что done, что ждёт человека, что устарело, решения."""
    c = st["counts"]
    lines = [f"[bulochka] Проект {st['slug']}: {progress_line(st)} ({st['step_status']})."]
    if st["done_steps"]:
        lines.append("Готово, не повторять: " + ", ".join(st["done_steps"]) + ".")
    lines.append(f"Реестры: утверждений {c['claims']} (проверено {c['verified']}), источников {c['sources']}, "
                 f"возражений открыто {c['objections_open']}, отказов {c['refusals']}, долг доказательств {c['evidence_debt']}.")
    if st["open_gates"]:
        lines.append("Ждёт ответа человека: " + "; ".join(
            f"{g['id']} [{g['kind']}] «{_short(g['question'], 120)}»" for g in st["open_gates"][:5]))
    if st["uncertain"]:
        lines.append("Незавершённые операции (uncertain, без автоповтора — решает человек): " + "; ".join(
            f"{u['id']} {u['operation']}/{u.get('step') or '-'}" for u in st["uncertain"][:8]))
    if st["stale"]:
        lines.append("Устарело: " + ", ".join(
            (f"{x['id']} ({x['age_days']} дн.)" if x["kind"] == "source" else f"{x['id']} в основании {x['of']} ({x['status']})")
            for x in st["stale"][:10]))
    if st["decisions"]:
        lines.append("Решения: " + "; ".join(
            f"{d['id']} {d['status']}" + (f" → {d['chosen_option']}" if d.get("chosen_option") else "")
            + f" «{_short(d.get('question'), 80)}»" for d in st["decisions"][:10]))
    if st["answered_gates"]:
        lines.append("Ответы человека: " + "; ".join(
            f"{g['id']} [{g['kind']}] {_short(g['answer'], 100)}" for g in st["answered_gates"][-8:]))
    if st["sources_frozen"]:
        lines.append("Сбор источников заморожен после синтеза (до шага revise).")
    if st.get("tokens"):
        lines.append(f"Токенов за проект: {st['tokens']}.")
    lines.append("Далее: " + st["next"]["why"])
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 40].rsplit("\n", 1)[0] + "\n… (полностью: bulochka status)"
    return text


def state_md(st: Dict[str, Any]) -> str:
    mark = {"done": "✓", "in_progress": "▶", "uncertain": "?", "failed": "✗", "pending": "·"}
    out = [f"# Состояние проекта {st['slug']}", "",
           f"Обновлено: {core.now_iso()}. Файл пишет `bulochka`, руками не править.", "",
           f"**{progress_line(st)}**", "", "| № | Шаг | Статус | Результат |", "|---|---|---|---|"]
    for sid in STEP_IDS:
        i = "8–9" if sid == "review" else STEP_N[sid]
        s = st["steps"][sid]
        cur = " ←" if sid == st["step"] else ""
        out.append(f"| {i} | {STEP_TITLE[sid]} (`/{STEP_SKILL[sid]}`){cur} | {mark[s['status']]} {s['status']} | "
                   f"{s.get('output') or ''} |")
    for kind, (at, before, title) in GATES.items():
        g = [x for x in st["answered_gates"] if x["kind"] == kind]
        o = [x for x in st["open_gates"] if x["kind"] == kind]
        status = f"✓ {g[-1]['id']}: {_short(g[-1]['answer'], 80)}" if g else (f"ждёт {o[0]['id']}" if o else "не задан")
        out.append(f"\nВорота «{title}» (после «{STEP_TITLE[at]}»): {status}")
    c = st["counts"]
    out += ["", "## Реестры", "",
            f"Источников {c['sources']} · утверждений {c['claims']} (проверено {c['verified']}) · "
            f"возражений открыто {c['objections_open']} · вариантов {c['options']} · решений {c['decisions']} · "
            f"отказов {c['refusals']} · долг доказательств {c['evidence_debt']}"]
    if st["uncertain"]:
        out += ["", "## Незавершённые операции (решает человек)", ""]
        out += [f"- {u['id']} `{u['operation']}` шаг {u.get('step') or '-'} с {u.get('ts')}: "
                f"`bulochka journal resolve {u['id']} --as committed|failed`" for u in st["uncertain"]]
    if st["stale"]:
        out += ["", "## Устарело", ""]
        out += [f"- {x['id']}: " + (f"{x['age_days']} дн. при сроке {x['ttl_days']}" if x["kind"] == "source"
                                    else f"в основании {x['of']}, статус {x['status']}") for x in st["stale"]]
    out += ["", f"**Далее:** {st['next']['why']}", ""]
    return "\n".join(out)


def resume_md(st: Dict[str, Any], slug: Optional[str], reason: str = "pause") -> str:
    from . import journal
    recent = [r for r in journal.records(slug) if r.get("status") == "committed"
              and not str(r.get("operation", "")).startswith("session.")][-10:]
    out = [f"# Продолжить отсюда — {st['slug']}", "",
           f"Записано: {core.now_iso()} ({reason}).", "",
           f"**Где остановились:** {progress_line(st)}, статус шага: {st['step_status']}.", ""]
    if st["done_steps"]:
        out.append("**Сделано (не повторять):** " + ", ".join(
            f"{STEP_TITLE[s]}" + (f" → {st['steps'][s]['output']}" if st['steps'][s].get('output') else "")
            for s in st["done_steps"]) + ".")
        out.append("")
    if st["open_gates"]:
        out.append("**Ждёт ответа человека:**")
        out += [f"- {g['id']} [{g['kind']}]: {g['question']}" for g in st["open_gates"]]
        out.append("")
    if st["answered_gates"]:
        out.append("**Ответы человека:**")
        out += [f"- {g['id']} [{g['kind']}]: {g['answer']}" for g in st["answered_gates"]]
        out.append("")
    if st["decisions"]:
        out.append("**Решения:**")
        out += [f"- {d['id']} {d['status']}" + (f" → {d['chosen_option']}" if d.get("chosen_option") else "")
                + f": {d.get('question')}" for d in st["decisions"]]
        out.append("")
    if st["uncertain"]:
        out.append("**Незавершённые операции — решает человек, не переисполнять:**")
        out += [f"- {u['id']} {u['operation']} (шаг {u.get('step') or '-'})" for u in st["uncertain"]]
        out.append("")
    if recent:
        out.append("**Последние завершённые операции:**")
        out += [f"- {r['id']} {r.get('operation')} {r.get('step') or ''} {r.get('output_path') or ''}".rstrip()
                for r in recent]
        out.append("")
    if slug or st.get("slug"):
        for text in ext.render_sections(RESUME_SECTIONS, slug or st["slug"]):
            out += [text, ""]
    out += [f"**Следующее действие:** {st['next']['why']}", "",
            "Чтобы продолжить в новом чате: скажите «продолжай» (или `/resume-run`).", ""]
    return "\n".join(out)


def refresh_state(slug: Optional[str] = None) -> Dict[str, Any]:
    """Пересобрать state/state.json и RUN_STATUS.md из журнала и реестров."""
    slug = slug or core.current_slug()
    st = compute(slug)
    persistent = load_state_file(slug)
    doc = {
        "slug": slug,
        "created_at": persistent.get("created_at") or core.now_iso(),
        "updated_at": core.now_iso(),
        "brief": persistent.get("brief", "order.md"),
        "step": st["step"],
        "step_n": st["step_n"],
        "steps_total": st["steps_total"],
        "step_status": st["step_status"],
        "steps": {k: v["status"] for k, v in st["steps"].items()},
        "waiting_gate": st["waiting_gate"],
        "blocked_by_gate": st["blocked_by_gate"],
        "open_gates": [g["id"] for g in st["open_gates"]],
        "uncertain": [u["id"] for u in st["uncertain"]],
        "sources_frozen": st["sources_frozen"],
        "counts": st["counts"],
    }
    core.atomic_write_json(core.state_dir(slug) / "state.json", doc)
    core.atomic_write(core.project_dir(slug) / "RUN_STATUS.md", state_md(st))
    return st


def receipt_sections(slug: str) -> List[str]:
    """Тексты разделов квитанции от расширений (RECEIPT_SECTIONS)."""
    return ext.render_sections(RECEIPT_SECTIONS, slug)


def write_resume(slug: Optional[str] = None, reason: str = "pause") -> Path:
    slug = slug or core.current_slug()
    st = refresh_state(slug)
    p = core.project_dir(slug) / "RESUME.md"
    core.atomic_write(p, resume_md(st, slug, reason))
    return p


# ---------- заморозка источников (state/no_web.flag, читает хук guard_web) ----------

def no_web_flag(slug: Optional[str] = None) -> Path:
    return core.state_dir(slug) / "no_web.flag"


def set_no_web(on: bool, slug: Optional[str] = None, reason: str = "") -> bool:
    """Поставить/снять заморозку WebFetch/WebSearch (и в субагентах). Возвращает новое значение.
    Ставится автоматически при `journal begin` шага strategy, снимается при begin шага revise."""
    f = no_web_flag(slug)
    if on:
        if not f.exists():
            core.atomic_write(f, f"{core.now_iso()} {reason or 'сбор источников закрыт после синтеза'}\n")
    elif f.exists():
        f.unlink()
    return on


def on_step_begin(step: Optional[str], slug: Optional[str] = None) -> None:
    if step == "strategy":
        set_no_web(True, slug, "начат шаг strategy")
    elif step == "revise":
        set_no_web(False, slug)


# ---------- команды ----------

def new_project(slug: str, brief: str) -> Path:
    from . import journal
    if not SLUG_RE.match(slug or ""):
        raise core.BulochkaError("slug: латиница в нижнем регистре, цифры, «-» и «_», до 64 символов.")
    pdir = core.projects_dir() / slug
    if pdir.exists():
        raise core.BulochkaError(f"Проект {slug} уже есть: {pdir}. Продолжить — `bulochka --project {slug} status`.")
    src = Path(brief).expanduser()
    if not src.is_file():
        raise core.BulochkaError(f"Файл брифа не найден: {src}")
    for sub in ("state", "sources", "report"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(pdir / "order.md"))
    core.atomic_write_json(pdir / "state" / "state.json",
                           {"slug": slug, "created_at": core.now_iso(), "brief": "order.md"})
    core.set_current_slug(slug)
    from .gates import adopt_pending_prompts
    adopt_pending_prompts(slug)
    journal.event("project.new", step="intake", actor="agent", slug=slug, output_path="order.md",
                  brief_sha256=core.sha256_text(src.read_text(encoding="utf-8", errors="replace")))
    refresh_state(slug)
    return pdir


def cmd_new(args) -> int:
    pdir = new_project(args.slug, args.brief)
    return core.emit("new", {"slug": args.slug, "path": str(pdir)}, as_json=args.json,
                     human=f"Проект {args.slug} создан: {pdir}. Бриф: order.md, состояние: RUN_STATUS.md.",
                     next_cmd="/order — шаг 1/11 «Постановка задачи»")


def cmd_status(args) -> int:
    st = compute()
    if args.for_hook:
        text = for_hook_text(st)
        if args.json:
            return core.emit("status", {"text": text, "chars": len(text)}, as_json=True)
        print(text)
        return core.EXIT_OK
    data = {k: st[k] for k in ("step", "step_n", "steps_total", "next", "open_gates", "counts", "stale",
                               "uncertain", "tokens", "usd_equiv")}
    data.update({k: st[k] for k in ("step_title", "step_status", "done_steps", "waiting_gate", "blocked_by_gate",
                                    "decisions", "sources_frozen", "rub_equiv", "weekly_limit_pct")})
    return core.emit("status", data, as_json=args.json, human=for_hook_text(st, max_chars=20000))


def cmd_next(args) -> int:
    st = compute()
    n = st["next"]
    return core.emit("next", n, as_json=args.json, human=n["why"])


def cmd_pause(args) -> int:
    from . import journal
    slug = core.current_slug()
    p = write_resume(slug, "pause")
    journal.event("session.pause", actor="agent", slug=slug, output_path="RESUME.md")
    return core.emit("pause", {"handoff": "RESUME.md", "path": str(p)}, as_json=args.json,
                     human=f"Записал {p}. В новом чате скажите «продолжай».")


def cmd_resume(args) -> int:
    slug = core.current_slug()
    st = compute(slug)
    p = core.project_dir(slug) / "RESUME.md"
    handoff = p.read_text(encoding="utf-8") if p.exists() else ""
    text = (handoff + "\n---\n" if handoff else "") + for_hook_text(st)
    return core.emit("resume", {"handoff": "RESUME.md" if handoff else None, "next": st["next"],
                                "text": text}, as_json=args.json, human=text)


def cmd_freeze(args) -> int:
    from . import journal
    slug = core.current_slug()
    on = not args.off
    set_no_web(on, slug, args.reason or "")
    journal.event("sources.freeze" if on else "sources.unfreeze", actor="agent", slug=slug, note=args.reason)
    refresh_state(slug)
    return core.emit("freeze", {"sources_frozen": on, "flag": "state/no_web.flag"}, as_json=args.json,
                     human="Сбор источников закрыт: WebFetch/WebSearch заблокированы хуком." if on
                     else "Сбор источников открыт.")


def register(sub) -> None:
    p = sub.add_parser("freeze", help="закрыть (или --off открыть) сбор источников из сети")
    p.add_argument("--off", action="store_true", help="снять заморозку")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("new", help="создать проект из брифа")
    p.add_argument("slug")
    p.add_argument("--brief", required=True, help="файл брифа (markdown/текст)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("status", help="состояние проекта")
    p.add_argument("--for-hook", dest="for_hook", action="store_true", help="короткая сводка для хука SessionStart")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="следующий шаг")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("pause", help="записать RESUME.md перед перерывом")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="показать RESUME.md и состояние")
    p.set_defaults(func=cmd_resume)
