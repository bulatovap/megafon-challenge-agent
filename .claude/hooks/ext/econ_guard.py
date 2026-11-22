#!/usr/bin/env python3
"""Цена решения — кодом, а не просьбой (живой прогон 21.11: агент пропускал смету, тест на разворот и ценник).

PreToolUse[Bash]:
- `journal begin --step plan|research|…` без `report/00_smeta.md` → deny: сначала смета `bulochka smeta`;
- `scripts/calc.py` (кроме `--help` и `schet` — счёт по числам брифа) без свежего `flip.run` в журнале (за 30 мин)
  → deny: сначала `bulochka flip`;
- свой скрипт `projects/*/calc/*.py` → deny: арифметика — `scripts/calc.py schet`, свои скрипты не запускаем.
PostToolUse[Bash] после успешного `decision lock D###`:
- сам пишет `report/90_price_tag.md` (`pricetag`) и закрывает смету (`smeta close`, факт в историю);
- в контекст — напоминание собрать первый экран (`firstscreen D### --answers …`): ответы пишет агент.
Сбой → incidents.jsonl, exit 0 (через диспетчер).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import _econ_hook as h

LATER_STEPS = ("plan", "research", "situation", "options", "strategy", "validation-plan", "review", "decide")
BEGIN_RE = re.compile(r"journal\s+begin\s+--step\s+(" + "|".join(re.escape(s) for s in LATER_STEPS) + r")\b")
CALC_RE = re.compile(r"scripts/calc\.py\b")
SCHET_RE = re.compile(r"scripts/calc\.py\s+schet\b")
OWN_CALC_RE = re.compile(r"projects/\S+/calc/\S+\.py\b")
LOCK_RE = re.compile(r"decision\s+lock\s+(D\d+)")
FLIP_FRESH = timedelta(minutes=30)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def context(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, ensure_ascii=False))


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def flip_is_fresh(slug):
    """Тест на разворот был на ЭТОМ шаге и не старше 30 мин (прогон 22.11: flip шага `cost-plan` «прикрывал» расчёты
    плана проверки)."""
    from tools.bulochka import journal, project
    now = datetime.now(timezone.utc)
    try:
        step = project.compute(slug).get("step")  # шаг в работе (а не шаг последней записи журнала)
    except Exception:  # noqa: BLE001
        step = None
    for r in reversed(journal.records(slug)):
        if r.get("operation") in ("flip.run", "calc.skipped"):
            if step and r.get("step") and r.get("step") != step:
                return False
            t = _ts(r.get("committed_at") or r.get("ts") or r.get("started_at"))
            return t is None or now - t <= FLIP_FRESH
    return False


def pre_tool(stdin, slug):
    from tools.bulochka import core
    cmd = str((stdin.get("tool_input") or {}).get("command") or "")
    if BEGIN_RE.search(cmd) and not (core.project_dir(slug) / "report" / "00_smeta.md").exists():
        deny("Сначала смета прогона — её ждёт продакт: `python3 -m tools.bulochka smeta --brief "
             f"projects/{slug}/order.md`, покажи карточку report/00_smeta.md и задай её ворота «Запускаем по смете?».")
        return
    if OWN_CALC_RE.search(cmd):
        deny("Свои скрипты в projects/*/calc/ не запускаем (права их не пропустят). Арифметику по числам брифа "
             "считай `python3 scripts/calc.py schet \"(plan - fact) / plan\" --var plan=… --var fact=… --json`; "
             "MDE, выборку, когорты, LTV — подкомандами calc.py.")
        return
    if CALC_RE.search(cmd) and not SCHET_RE.search(cmd) and "--help" not in cmd and not flip_is_fresh(slug):
        deny("Перед расчётом — тест на разворот (skill /flip-check): `python3 -m tools.bulochka flip --question \"…\" "
             "--switch \"<порог переворота>\" --plausible \"<правдоподобный диапазон>\"`. Если расчёт не меняет выбор "
             "ни при каком исходе — не считаешь, экономия уйдёт в журнал.")


def post_tool(stdin, slug):
    cmd = str((stdin.get("tool_input") or {}).get("command") or "")
    m = LOCK_RE.search(cmd)
    if not m:
        return
    from tools.bulochka import core
    did = m.group(1)
    d = core.get("decision", did, slug) or {}
    if d.get("status") != "locked":
        return
    done = []
    try:
        from tools.bulochka import x_pricetag
        x_pricetag.build(did, slug)
        done.append("ценник report/90_price_tag.md")
    except Exception as e:  # noqa: BLE001
        done.append(f"ценник не собран ({type(e).__name__})")
    try:
        from tools.bulochka import x_smeta
        x_smeta.close(slug)
        done.append("смета закрыта — факт прогона в истории")
    except Exception as e:  # noqa: BLE001
        done.append(f"смета не закрыта ({type(e).__name__}: {e})")
    context("PostToolUse", f"[экономика] {did} зафиксировано: " + "; ".join(done) + ". Покажи продакту ценник "
            f"(report/90_price_tag.md) и собери первый экран: `python3 -m tools.bulochka firstscreen {did} --answers "
            "'<JSON: ответы на четыре вопроса руководства со ссылками C###/D###>'`.")


def main():
    stdin = h.read_stdin()
    slug = h.slug()
    if not slug or (stdin.get("tool_name") or "") != "Bash":
        return 0
    ev = stdin.get("hook_event_name")
    if ev == "PreToolUse":
        pre_tool(stdin, slug)
    elif ev == "PostToolUse":
        post_tool(stdin, slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
