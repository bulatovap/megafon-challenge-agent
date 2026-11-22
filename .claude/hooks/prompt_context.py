#!/usr/bin/env python3
"""UserPromptSubmit: строка прогресса и журнал сообщений человека.

1. Сообщение человека дописывается в projects/<slug>/state/human_prompts.jsonl — по нему `bulochka answer`
   сверяет, что ответ на воротах записан дословно, а не пересказан. Проекта ещё нет (первая реплика создаёт его
   в том же вызове) — реплика ждёт в projects/.pending_prompts.jsonl, `bulochka new` переносит её в журнал.
2. В additionalContext — строка, с которой агент начинает ответ: «Шаг N/11 · <шаг>» (без проекта — шаг 1).

Не блокирует ввод (exit 0). Внутренняя ошибка → incidents.jsonl.
"""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import _common  # noqa: E402

MAX_TEXT = 4000


def progress(slug):
    from tools.bulochka import project
    if not slug:
        n, title = 1, project.STEP_TITLE["intake"]
        return f"Шаг {n}/{project.STEPS_TOTAL} · {title}"
    st = project.compute(slug)
    return f"Шаг {st['step_n']}/{st['steps_total']} · {st['step_title']}"


def handle(data):
    from tools.bulochka import core
    from tools.bulochka.gates import HUMAN_PROMPTS, stash_prompt
    slug = _common.slug()
    text = str(data.get("prompt") or "").strip()
    if text:
        rec = {"ts": core.now_iso(), "text": text[:MAX_TEXT], "session_id": data.get("session_id")}
        if slug:
            core.append_jsonl(core.state_dir(slug) / HUMAN_PROMPTS, rec)
        else:
            stash_prompt(rec)
    line = progress(slug)
    _common.emit({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": f"[bulochka] Начни ответ строкой «{line}» (если по ходу перешёл к другому шагу — "
                             f"следующий ответ начни с нового номера)."}})
    return 0


if __name__ == "__main__":
    _common.run(handle)
