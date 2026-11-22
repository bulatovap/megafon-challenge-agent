#!/usr/bin/env python3
"""SessionEnd: сохранить состояние (RUN_STATUS.md, state.json, RESUME.md) и записать session.end.

clean_exit = у этой сессии не осталось незавершённых операций; оставшиеся started → uncertain.
"""
from __future__ import annotations

import _common


def handle(data):
    from tools.bulochka import journal, project
    s = _common.slug()
    if not s:
        return 0
    sid = data.get("session_id") or journal.current_session_id(s)
    reason = data.get("reason") or "other"
    left = journal.mark_session_uncertain(s, only_session=sid, reason=f"сессия завершилась ({reason}) до commit")
    project.write_resume(s, reason=f"конец сессии: {reason}")
    journal.event("session.end", actor="hook", slug=s, session_id=sid, clean_exit=not left, end_reason=reason,
                  uncertain_ids=left or None)
    project.refresh_state(s)
    return 0


if __name__ == "__main__":
    _common.run(handle)
