"""Изоляция тестов базового слоя от расширений: папки x_*.py и ext-хуков — пустые временные.

Тесты базового слоя в копии с модулями x_*.py и `.claude/hooks/ext/*.py` дают тот же результат, что в чистом базовом слое:
BULOCHKA_EXT_DIR и BULOCHKA_EXT_HOOKS_DIR указывают на пустые папки (их наследуют и хуки в subprocess),
ext.reset() убирает функции выгруженных модулей из реестров базового слоя. После теста всё возвращается.

Изолируются только тесты базового слоя. Тесты расширений — модули `test_x_*.py` (и классы из `_x_*.py`, `x_*.py`) —
наследуют те же окружения, но им нужны свои модули, поэтому для них изоляция выключена. Явно задать можно
атрибутом класса `EXT_ISOLATED = True | False`.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from tools.bulochka import ext  # noqa: E402

KEYS = ("BULOCHKA_EXT_DIR", "BULOCHKA_EXT_HOOKS_DIR")


EXT_TEST_PREFIXES = ("test_x_", "_x_", "x_")


def wants_isolation(case) -> bool:
    flag = getattr(case, "EXT_ISOLATED", None)
    if flag is not None:
        return bool(flag)
    mod = (type(case).__module__ or "").rsplit(".", 1)[-1]
    return not mod.startswith(EXT_TEST_PREFIXES)


def isolate_ext(case) -> None:
    """Вызывать в setUp; откат — через addCleanup (после tearDown). Тест расширения — без изоляции."""
    if not wants_isolation(case):
        return
    saved = {k: os.environ.get(k) for k in KEYS}
    tmp = Path(tempfile.mkdtemp(prefix="ext-empty-"))
    (tmp / "modules").mkdir()
    (tmp / "hooks").mkdir()
    os.environ["BULOCHKA_EXT_DIR"] = str(tmp / "modules")
    os.environ["BULOCHKA_EXT_HOOKS_DIR"] = str(tmp / "hooks")
    ext.reset()

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ext.reset()
        shutil.rmtree(tmp, ignore_errors=True)

    case.addCleanup(restore)
