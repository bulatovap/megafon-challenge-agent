#!/usr/bin/env python3
"""PreToolUse[WebFetch|WebSearch]: allowlist доменов, ask для серых, deny для секретов и после синтеза.

- Секрет в URL/запросе (token=, api_key=, password=, sk-…) → deny.
- Сбор источников заморожен (флаг projects/<slug>/state/no_web.flag; ставит begin шага `playbook` или
  `bulochka freeze`, снимает begin шага `reforecast` или `bulochka freeze --off`) → deny, в т.ч. внутри субагентов.
- WebFetch: домен из config/sources.allow (сам или поддомен) → allow; иначе → ask (серый домен).
- WebSearch: без решения (обычные права Claude Code), кроме заморозки и секретов.
- Каждое ask/deny пишется в журнал (web.guard): Claude Code сам отказ в `-p` не логирует.
Формат config/sources.allow: домен на строку, `#` — комментарий.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import _common

SECRET = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|session[_-]?id)=[^&\s]{6,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+[A-Za-z0-9._-]{20,}")


def decision(kind, reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def allowlist():
    from tools.bulochka import core
    p = core.config_path("sources.allow")
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        d = line.split("#", 1)[0].strip().lower()
        if not d:
            continue
        if "://" in d:
            d = urlparse(d).hostname or ""
        d = d.lstrip("*.").strip("/")
        if d.startswith("www."):
            d = d[4:]
        if d:
            out.append(d)
    return out


def host_of(url):
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_allowed(host, allow):
    return bool(host) and any(host == d or host.endswith("." + d) for d in allow)


def frozen():
    from tools.bulochka import project
    s = _common.slug()
    return bool(s) and project.no_web_flag(s).exists()


def log_decision(data, kind, reason, target):
    """ask/deny → журнал (событие web.guard) — иначе отказ нигде не виден."""
    from tools.bulochka import journal
    s = _common.slug()
    if not s:
        return
    journal.event("web.guard", actor="hook", slug=s, session_id=data.get("session_id"),
                  decision=kind, reason=reason, tool=data.get("tool_name"), target=target[:300],
                  agent_id=data.get("agent_id"), agent_type=data.get("agent_type"))


def respond(data, kind, reason, target):
    if kind in ("ask", "deny"):
        log_decision(data, kind, reason, target)
    _common.emit(decision(kind, reason))


def handle(data):
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    url = str(ti.get("url") or "")
    query = str(ti.get("query") or "")
    text = url + " " + query + " " + str(ti.get("prompt") or "")
    if SECRET.search(text):
        reason = "В адресе или запросе похоже на секрет (ключ, токен, пароль) — не отправляю наружу."
        _common.incident("web_secret_blocked", reason, data, url=url[:300])
        respond(data, "deny", reason, "[скрыто: секрет]")
        return 0
    if frozen():
        reason = ("Сбор источников закрыт после синтеза: стратегия строится на уже собранных S###. "
                  "Новые данные — через /reforecast (шаг 11), тогда поиск снова открыт.")
        respond(data, "deny", reason, url or query)
        return 0
    if tool == "WebFetch" and url:
        if url.startswith("file://"):
            return 0
        host = host_of(url)
        if is_allowed(host, allowlist()):
            respond(data, "allow", f"{host} в config/sources.allow", url)
        else:
            respond(data, "ask", f"{host or url} нет в config/sources.allow — серый домен. "
                                         "Открыть? Источник получит пониженный грейд, если не подтвердится.", url)
    return 0


if __name__ == "__main__":
    _common.run(handle)
