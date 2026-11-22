#!/usr/bin/env python3
"""PostToolUse: сигнал «хватит искать» посреди /sourcing — две пачки источников подряд без новых material.

Срабатывает только после команды `bulochka source …` на шаге research и один раз на число источников
(state/saturation.json). Сбой → incidents.jsonl, exit 0.
"""
from __future__ import annotations

import json
import sys

import _econ_hook as h

EVENT = "PostToolUse"


def handle(stdin):
    if stdin.get("hook_event_name") not in (None, EVENT) or stdin.get("tool_name") not in (None, "Bash"):
        return None
    cmd = str((stdin.get("tool_input") or {}).get("command") or "")
    if "tools.bulochka" not in cmd or " source " not in f" {cmd} ":
        return None
    slug = h.slug()
    if not slug:
        return None
    from tools.bulochka import core, cost, x_saturation
    if cost.current_step(slug) != "research":
        return None
    r = x_saturation.batches(slug=slug)
    if not r["stop"]:
        return None
    path = core.state_dir(slug) / "saturation.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get("signalled_at_sources") == r["sources"]:
        return None
    core.atomic_write_json(path, {"signalled_at_sources": r["sources"], "ts": core.now_iso(),
                                  "batches": r["batches"]})
    return r["phrase"]


def main():
    try:
        msg = handle(h.read_stdin())
        if msg:
            h.context(EVENT, msg)
    except Exception as e:  # noqa: BLE001
        h.incident("post_tool_saturation", f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
