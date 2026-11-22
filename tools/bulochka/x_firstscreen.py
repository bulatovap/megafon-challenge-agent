"""Первый экран для руководства: `bulochka firstscreen D### --answers <JSON>` → report/01_first_screen.md.

Решение одним предложением и ответы на четыре вопроса руководства. Проверка кодом: у каждого ответа есть
ссылка на C### или D### из реестра; ответ без ссылки или со ссылкой в никуда — ошибка, файл не пишется.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import core

QUESTIONS = [
    ("what", "Что делаем?"),
    ("why", "Почему это, а не другое?"),
    ("money", "Что это даёт в деньгах и сколько стоило решение?"),
    ("check", "Как поймём, что сработало, и когда пересматриваем?"),
]
REF = re.compile(r"\b[CD]\d{3,}\b")


def check_answers(answers: Dict[str, str], slug: Optional[str] = None) -> List[Dict[str, Any]]:
    claims, decisions = core.latest("claim", slug), core.latest("decision", slug)
    errors = []
    for key, q in QUESTIONS:
        text = str(answers.get(key) or "").strip()
        refs = REF.findall(text)
        if not text:
            errors.append({"rule": "FIRST_SCREEN", "level": "error", "ids": [key], "msg": f"нет ответа на «{q}»"})
        elif not refs:
            errors.append({"rule": "FIRST_SCREEN", "level": "error", "ids": [key],
                           "msg": f"ответ на «{q}» без ссылки на C### или D###"})
        else:
            missing = [r for r in refs if r not in (claims if r[0] == "C" else decisions)]
            if missing:
                errors.append({"rule": "FIRST_SCREEN", "level": "error", "ids": missing,
                               "msg": f"ответ на «{q}» ссылается на несуществующие {', '.join(missing)}"})
    return errors


def build(decision_id: str, answers: Dict[str, str], sentence: Optional[str] = None,
          slug: Optional[str] = None) -> Dict[str, Any]:
    slug = slug or core.current_slug()
    pdir = core.project_dir(slug)
    d = core.get("decision", decision_id, slug)
    if d is None:
        raise core.BulochkaError(f"Решение {decision_id} не найдено.")
    if not sentence:
        o = core.get("option", d.get("chosen_option") or "", slug) if d.get("chosen_option") else None
        if o is None:
            raise core.BulochkaError("Нужен --sentence: у решения ещё нет выбранного варианта.")
        sentence = f"{o.get('title')} [{decision_id}, {o['id']}]"
    errors = check_answers(answers, slug)
    if errors:
        return {"ok": False, "errors": errors}
    lines = [f"# Решение одним предложением", "", sentence, ""]
    for key, q in QUESTIONS:
        lines += [f"**{q}** {answers[key].strip()}", ""]
    out = pdir / "report" / "01_first_screen.md"
    core.atomic_write(out, "\n".join(lines))
    return {"ok": True, "errors": [], "path": str(out), "sentence": sentence}


def cmd_firstscreen(args) -> int:
    try:
        answers = json.loads(args.answers)
    except ValueError:
        raise core.BulochkaError('--answers: JSON {"what": "…", "why": "…", "money": "…", "check": "…"}.')
    if not isinstance(answers, dict):
        raise core.BulochkaError("--answers: ожидается JSON-объект.")
    r = build(args.decision_id, answers, args.sentence)
    human = f"Первый экран: {r['path']}" if r["ok"] else "Первый экран не записан: у ответа нет проверяемой ссылки."
    return core.emit("firstscreen", {k: v for k, v in r.items() if k != "errors"}, ok=r["ok"], errors=r["errors"],
                     human=human, as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("firstscreen", help="первый экран: решение одним предложением и 4 ответа со ссылками")
    p.add_argument("decision_id")
    p.add_argument("--answers", required=True, help='JSON {"what","why","money","check"} — в каждом C### или D###')
    p.add_argument("--sentence", help="решение одним предложением (по умолчанию — выбранный вариант)")
    p.set_defaults(func=cmd_firstscreen)
