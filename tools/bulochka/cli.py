"""Точка входа CLI: `python3 -m tools.bulochka <команда> [--json]`.

Команды регистрируют модули пакета: каждый модуль из COMMAND_MODULES определяет
`register(subparsers)` и вешает на свои подпарсеры `set_defaults(func=...)`.
func(args) -> int (код выхода). Новый модуль базового слоя = одна строка в COMMAND_MODULES.
Модули-расширения `tools/bulochka/x_*.py` с `register(subparsers)` подхватываются сами (см. ext.py);
их ошибка пишется в stderr и не роняет CLI.
"""
from __future__ import annotations

import argparse
import importlib
import sys

from . import core, ext

# Модули команд CLI
COMMAND_MODULES = [
    "project",      # new, status, next, pause, resume — срез «состояние»
    "journal",      # journal begin/commit/fail/resolve — срез «состояние»
    "sources",      # source add/refresh/exclude, freshness — срез «леджер»
    "claims",       # claim add/supersede — срез «леджер»
    "objections",   # objection add/resolve — срез «леджер»
    "decisions",    # option *, decision * — срез «леджер»
    "gates",        # ask, answer — срез «состояние»
    "verify",       # verify, check, refusals — срез «леджер»
    "cost",         # cost — срез «экономика»
    "export",       # export — срез «экономика»
    "doctor",       # doctor — срез «экономика»
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bulochka", description="Базовый слой стратега: реестры, проверки, состояние.")
    p.add_argument("--json", action="store_true", help="машиночитаемый ответ")
    p.add_argument("--project", help="slug проекта (по умолчанию projects/CURRENT)")
    sub = p.add_subparsers(dest="cmd")
    for name in COMMAND_MODULES:
        try:
            mod = importlib.import_module(f".{name}", __package__)
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(f".{name}"):
                continue  # модуль ещё не написан — команда просто недоступна
            raise
        mod.register(sub)
    for mod in ext.load():
        reg = getattr(mod, "register", None)
        if not callable(reg):
            continue
        try:
            reg(sub)
        except Exception as e:  # noqa: BLE001 — команда расширения недоступна, базовый слой работает
            ext.warn(f"модуль {mod.__name__.rsplit('.', 1)[-1]}.py: register не выполнен: {type(e).__name__}: {e}")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    # --json глобальный, но агенты пишут его и после подкоманды (`check --json`):
    # вынимаем из argv и ставим флаг после разбора, чтобы годились обе позиции.
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    args = parser.parse_args(argv)
    if want_json:
        args.json = True
    if args.project:
        import os
        os.environ["BULOCHKA_PROJECT"] = args.project
    if not getattr(args, "func", None):
        parser.print_help()
        return core.EXIT_USAGE
    try:
        return int(args.func(args) or 0)
    except core.BulochkaError as e:
        if getattr(args, "json", False):
            return core.emit(args.cmd or "", ok=False, errors=[{"rule": "USAGE", "level": "error", "msg": str(e)}],
                             as_json=True, code=core.EXIT_USAGE)
        print(str(e), file=sys.stderr)
        return core.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
