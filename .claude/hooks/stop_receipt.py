#!/usr/bin/env python3
"""Stop: projects/<slug>/STOP_RECEIPT.md — что сделано за сессию, что ждёт человека, что дальше. Не блокирует.

Разделы расширений: функции из project.RECEIPT_SECTIONS (fn(slug) -> str; модули x_*.py регистрируют их
при импорте) встают после основных разделов, перед «Далее». Сбой раздела не ломает квитанцию.
"""
from __future__ import annotations

import _common


def handle(data):
    from tools.bulochka import core, journal, project
    s = _common.slug()
    if not s:
        return 0
    sid = data.get("session_id")
    st = project.compute(s)
    mine = [r for r in journal.records(s) if r.get("session_id") == sid
            and r.get("operation") not in ("session.start", "session.end")]
    done = [r for r in mine if r.get("status") == "committed"]
    seen, uniq = set(), []
    for r in reversed(done):
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            uniq.append(r)
    uniq.reverse()
    c = st["counts"]
    lines = [f"# Квитанция остановки — {s}", "", f"Время: {core.now_iso()}. Сессия: {sid or '—'}.", "",
             f"**{project.progress_line(st)}** ({st['step_status']})", "",
             f"Реестры: источников {c['sources']}, утверждений {c['claims']} (проверено {c['verified']}), "
             f"возражений открыто {c['objections_open']}, отказов {c['refusals']} (ждут {c['refusals_pending']}), "
             f"долг доказательств {c['evidence_debt']}.", ""]
    lines.append("## Сделано в этой сессии")
    lines += [f"- {r['id']} {r.get('operation')} {r.get('step') or ''} {r.get('output_path') or ''}".rstrip()
              for r in uniq[-20:]] or ["- ничего не завершено"]
    if st["open_gates"]:
        lines += ["", "## Ждёт ответа человека"]
        lines += [f"- {g['id']} [{g['kind']}]: {g['question']}" for g in st["open_gates"]]
    if st["uncertain"]:
        lines += ["", "## Незавершённые операции (решает человек)"]
        lines += [f"- {u['id']} {u['operation']} шаг {u.get('step') or '-'}" for u in st["uncertain"]]
    for sect in project.receipt_sections(s):
        lines += ["", sect]
    lines += ["", f"**Далее:** {st['next']['why']}", ""]
    core.atomic_write(core.project_dir(s) / "STOP_RECEIPT.md", "\n".join(lines))
    return 0


if __name__ == "__main__":
    _common.run(handle)
