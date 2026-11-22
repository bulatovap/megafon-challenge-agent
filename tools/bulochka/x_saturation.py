"""Останов сбора по дешёвому сигналу: `bulochka saturation [--batch N]`.

Без LLM-судьи: источники по порядку делятся на пачки по N; для каждой пачки считаем новые и изменённые
material-утверждения, которые на неё опираются. Две полные пачки подряд без новых material — сигнал «хватит».
Ext-хук post_tool_saturation подаёт сигнал агенту посреди шага /sourcing (один раз на число источников).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import core

DEFAULT_BATCH = 3
EMPTY_IN_ROW = 2


def batches(batch: int = DEFAULT_BATCH, slug: Optional[str] = None) -> Dict[str, Any]:
    sources = sorted((s for s in core.latest("source", slug).values() if s.get("status", "active") == "active"),
                     key=lambda s: s["id"])
    index = {s["id"]: i // batch for i, s in enumerate(sources)}
    n_full = len(sources) // batch
    new = [0] * (n_full + 1)
    changed = [0] * (n_full + 1)
    versions: Dict[str, int] = {}
    for rec in core.read_jsonl(core.entity_path("claim", slug)):
        if rec.get("id"):
            versions[rec["id"]] = versions.get(rec["id"], 0) + 1
    for c in core.latest("claim", slug).values():
        if not c.get("material"):
            continue
        b = [index[s] for s in c.get("source_ids") or [] if s in index]
        if not b:
            continue
        k = min(b)  # пачка, в которой утверждение появилось
        if c.get("supersedes") or versions.get(c["id"], 1) > 1:
            changed[k] += 1
        else:
            new[k] += 1
    rows = [{"batch": i + 1, "sources": [s["id"] for s in sources[i * batch:(i + 1) * batch]],
             "new_material": new[i], "changed_material": changed[i], "full": i < n_full}
            for i in range(len(new)) if i < n_full or (i == n_full and len(sources) % batch)]
    full = [r for r in rows if r["full"]]
    tail = full[-EMPTY_IN_ROW:]
    stop = len(tail) == EMPTY_IN_ROW and all(r["new_material"] + r["changed_material"] == 0 for r in tail)
    return {"batch_size": batch, "sources": len(sources), "batches": rows, "stop": stop,
            "phrase": (f"Сбор насытился: последние {EMPTY_IN_ROW} пачки по {batch} источника не дали ни одного "
                       f"нового или изменённого material-утверждения. Хватит искать — переходи к картине ситуации "
                       f"(или назови вопрос плана, по которому ещё пусто)." if stop else
                       f"Сбор продолжается: источников {len(sources)}, пачек {len(full)}.")}


def cmd_saturation(args) -> int:
    r = batches(args.batch)
    lines = [f"Пачка {b['batch']}: +{b['new_material']} новых, {b['changed_material']} изменённых material "
             f"({', '.join(b['sources'])})" for b in r["batches"]] + [r["phrase"]]
    return core.emit("saturation", r, human="\n".join(lines), as_json=getattr(args, "json", False))


def register(sub) -> None:
    p = sub.add_parser("saturation", help="насыщение сбора: новые material-утверждения на пачку источников")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="источников в пачке")
    p.set_defaults(func=cmd_saturation)
