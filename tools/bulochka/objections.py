"""Возражения: objection add / resolve / list."""
from __future__ import annotations

from . import core
from .sources import split_ids

SEVERITIES = ("high", "medium", "low")
AUTHORS = ("red-team", "human", "agent")


def cmd_add(args) -> int:
    target = args.target
    if target[:1] not in ("O", "D", "C") or not core.resolve_ref(target):
        raise core.BulochkaError(f"--target: {target} не найден (ожидается O###, D### или C###).")
    evidence = split_ids(args.evidence)
    for e in evidence:
        if not core.get("claim", e):
            raise core.BulochkaError(f"--evidence: {e} не найден.")
    if args.severity == "high" and not evidence:
        raise core.BulochkaError("Для severity=high нужен --evidence C### (на чём держится возражение).")
    rec = {"target_id": target, "text": args.text, "severity": args.severity, "evidence_ids": evidence,
           "status": "open", "resolution": None, "material": bool(args.material or args.severity == "high"),
           "seeded": bool(args.seeded), "author": args.author, "actor": "human" if args.author == "human" else "agent"}
    rec = core.put("objection", rec)
    return core.emit("objection add", {"id": rec["id"]}, human=f"Возражение {rec['id']} к {target} ({args.severity}).",
                     next_cmd=f"bulochka objection resolve {rec['id']} --status accepted|rejected --resolution \"…\"",
                     as_json=args.json)


def cmd_resolve(args) -> int:
    r = core.get("objection", args.id)
    if not r:
        raise core.BulochkaError(f"Возражение {args.id} не найдено.")
    if not (args.resolution or "").strip():
        raise core.BulochkaError("--resolution: «учтено в …» или «отклонено, потому что …».")
    upd = dict(r)
    upd.update({"status": args.status, "resolution": args.resolution})
    core.put("objection", upd)
    # авто-возражение A_CONFLICT закрывает связанный отказ
    for x in core.latest("refusal").values():
        if x.get("objection_id") == r["id"] and x.get("outcome") == "pending":
            xu = dict(x)
            xu["outcome"] = "accepted"
            core.put("refusal", xu)
    return core.emit("objection resolve", {"id": r["id"]}, human=f"{r['id']}: {args.status} — {args.resolution}.",
                     as_json=args.json)


def cmd_list(args) -> int:
    items = sorted(core.latest("objection").values(), key=lambda r: r["id"])
    if args.open:
        items = [r for r in items if r.get("status") == "open"]
    if args.target:
        items = [r for r in items if r.get("target_id") == args.target]
    lines = [f"{r['id']} → {r.get('target_id')} [{r.get('severity')}/{r.get('status')}] {r.get('text')}" for r in items]
    return core.emit("objection list", {"items": items}, human="\n".join(lines) or "Возражений нет.", as_json=args.json)


def register(sub) -> None:
    p = sub.add_parser("objection", help="возражения: add, resolve, list")
    ss = p.add_subparsers(dest="objection_cmd")

    a = ss.add_parser("add")
    a.add_argument("--target", required=True, help="O### / D### / C###")
    a.add_argument("--text", required=True)
    a.add_argument("--severity", required=True, choices=SEVERITIES)
    a.add_argument("--evidence", help="C### (через запятую); обязательно для high")
    a.add_argument("--author", choices=AUTHORS, default="red-team")
    a.add_argument("--material", action="store_true")
    a.add_argument("--seeded", action="store_true", help="только для eval-фикстур")
    a.set_defaults(func=cmd_add)

    r = ss.add_parser("resolve")
    r.add_argument("id")
    r.add_argument("--status", required=True, choices=("accepted", "rejected"))
    r.add_argument("--resolution", required=True)
    r.set_defaults(func=cmd_resolve)

    lst = ss.add_parser("list")
    lst.add_argument("--open", action="store_true")
    lst.add_argument("--target")
    lst.set_defaults(func=cmd_list)
