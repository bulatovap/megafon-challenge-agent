#!/usr/bin/env python3
"""PostCompact: сверка compact_summary с state/ → state/compact_gap.json.

Ключевые записи: текущий шаг, решения D###, ответы человека G###, открытые ворота, uncertain.
Чего нет в пересказе — попадает в missing; SessionStart[compact] вернёт это в контекст.
"""
from __future__ import annotations

import _common


def handle(data):
    from tools.bulochka import core, project
    s = _common.slug()
    if not s:
        return 0
    summary = data.get("compact_summary") or ""
    st = project.compute(s)
    items = []
    for d in st["decisions"]:
        what = f"решение {d['status']}" + (f" → {d['chosen_option']}" if d.get("chosen_option") else "") \
               + f": {d.get('question') or ''}"
        items.append({"id": d["id"], "what": what})
        if d.get("chosen_option"):
            items.append({"id": d["chosen_option"], "what": f"выбранный вариант в {d['id']}"})
    for g in st["answered_gates"]:
        items.append({"id": g["id"], "what": f"ответ человека [{g['kind']}]: {g['answer']}"})
    for g in st["open_gates"]:
        items.append({"id": g["id"], "what": f"ждёт ответа [{g['kind']}]: {g['question']}"})
    for u in st["uncertain"]:
        items.append({"id": u["id"], "what": f"uncertain {u['operation']} — решает человек"})
    present, missing = [], []
    for it in items:
        (present if it["id"] in summary else missing).append(it)
    step_ok = bool(st["step"]) and (st["step"] in summary or st["step_title"] in summary)
    if st["step"] and not step_ok:
        missing.insert(0, {"id": f"шаг {st['step_n']}/{st['steps_total']}",
                           "what": f"текущий шаг «{st['step_title']}»; готово: {', '.join(st['done_steps']) or '—'}"})
    gap = {"ts": core.now_iso(), "session_id": data.get("session_id"), "trigger": data.get("trigger"),
           "summary_chars": len(summary), "checked": len(items) + (1 if st["step"] else 0),
           "present": [i["id"] for i in present], "missing": missing}
    core.atomic_write_json(core.state_dir(s) / "compact_gap.json", gap)
    return 0


if __name__ == "__main__":
    _common.run(handle)
