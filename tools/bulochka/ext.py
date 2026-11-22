"""Точки расширения: модули `tools/bulochka/x_*.py`.

Каждый модуль загружается один раз за процесс, по алфавиту. Что он может объявить:
- `register(subparsers)` — свои команды CLI (подхватывает cli.build_parser);
- `EXTRA_RULES = {"X01": fn}` — правила quality gate, fn(ctx) -> [{"ids": [...], "msg": "..."}] (verify.run_check);
- при импорте — свои функции в реестры базового слоя (список REGISTRIES ниже):
  `decisions.PRE_CHOOSE`  fn(decision, option) -> отказ | None — перед выбором и фиксацией решения;
  `gates.PRE_ANSWER`      fn(gate, answer_text) -> отказ | None — перед записью ответа на воротах;
  `gates.POST_ANSWER`     fn(gate) -> None — после записи ответа;
  `project.RECEIPT_SECTIONS`   fn(slug) -> str — раздел квитанции остановки (STOP_RECEIPT.md);
  `export.ONE_PAGER_SECTIONS`  fn(slug) -> str — раздел одностраничника (`export --one-pager`).
  Базовый слой вызывает из реестра только функции модулей, загруженных из текущей папки расширений (см. active()).

Ошибка импорта модуля не роняет CLI: строка в stderr, модуль пропускается.
Папку можно подменить переменной BULOCHKA_EXT_DIR (для тестов): смена папки = reset() и загрузка заново.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List, Optional

PACKAGE = __package__ or "tools.bulochka"

_loaded: Optional[Dict[str, ModuleType]] = None
_loaded_dir: Optional[str] = None
errors: List[str] = []

# Реестры базового слоя, куда расширения добавляют функции при импорте: (модуль базового слоя, имя списка)
REGISTRIES = (("decisions", "PRE_CHOOSE"), ("gates", "PRE_ANSWER"), ("gates", "POST_ANSWER"),
              ("project", "RECEIPT_SECTIONS"), ("export", "ONE_PAGER_SECTIONS"))


def ext_dir() -> Path:
    env = os.environ.get("BULOCHKA_EXT_DIR")
    return Path(env) if env else Path(__file__).resolve().parent


def module_paths() -> List[Path]:
    d = ext_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("x_*.py") if p.is_file())


def warn(msg: str) -> None:
    errors.append(msg)
    try:
        print(f"bulochka: {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _same_file(mod: ModuleType, path: Path) -> bool:
    try:
        return Path(getattr(mod, "__file__", "") or "").resolve() == path.resolve()
    except OSError:
        return False


def load() -> List[ModuleType]:
    """Импортировать все x_*.py (один раз за процесс); вернуть успешно загруженные."""
    global _loaded, _loaded_dir
    cur = str(ext_dir())
    if _loaded is not None and _loaded_dir != cur:
        reset()
    if _loaded is None:
        _loaded = {}
        _loaded_dir = cur
        for path in module_paths():
            name = f"{PACKAGE}.{path.stem}"
            try:
                mod = sys.modules.get(name)
                if mod is not None and not _same_file(mod, path):
                    mod = None  # одноимённый модуль из другой папки — грузим заново
                if mod is None:
                    spec = importlib.util.spec_from_file_location(name, str(path))
                    if spec is None or spec.loader is None:
                        raise ImportError(f"не удалось прочитать {path.name}")
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[name] = mod
                    try:
                        spec.loader.exec_module(mod)
                    except BaseException:
                        sys.modules.pop(name, None)
                        raise
                _loaded[path.stem] = mod
            except Exception as e:  # noqa: BLE001 — расширение не должно ронять базовый слой
                warn(f"модуль {path.name} не загружен: {type(e).__name__}: {e}")
    return list(_loaded.values())


def _is_ext_module(modname: str) -> bool:
    return modname.startswith(f"{PACKAGE}.x_")


def _purge(stems: List[str]) -> None:
    """Убрать из реестров базового слоя функции модулей `stems`."""
    names = {f"{PACKAGE}.{s}" for s in stems}
    for modname, attr in REGISTRIES:
        mod = sys.modules.get(f"{PACKAGE}.{modname}")
        reg = getattr(mod, attr, None) if mod else None
        if not isinstance(reg, list):
            continue
        reg[:] = [fn for fn in reg if getattr(fn, "__module__", "") not in names]


def active(fns: List[Callable]) -> List[Callable]:
    """Функции реестра, которые базовый слой вызывает сейчас: функции базового слоя (и тестов) и функции модулей x_*.py,
    загруженных load() из текущей папки расширений. Функция модуля x_*, которого нет среди загруженных
    (другая папка, выгружен reset(), старая копия после повторного импорта), не вызывается."""
    load()  # сначала импорт: модули x_*.py дописывают реестр при импорте
    loaded = _loaded or {}
    out: List[Callable] = []
    for fn in list(fns):
        modname = getattr(fn, "__module__", "") or ""
        if not _is_ext_module(modname):
            out.append(fn)
            continue
        mod = loaded.get(modname.rsplit(".", 1)[-1])
        if mod is None:
            continue
        g = getattr(fn, "__globals__", None)
        if g is not None and g is not mod.__dict__:
            continue  # функция из другой копии модуля
        out.append(fn)
    return out


def render_sections(fns: List[Callable], slug: str) -> List[str]:
    """Тексты разделов расширений: fn(slug) -> str по порядку регистрации; пустые пропускаются,
    сбой раздела — строка в stderr, остальное собирается."""
    out: List[str] = []
    for fn in active(fns):
        try:
            text = fn(slug)
        except Exception as e:  # noqa: BLE001
            warn(f"раздел {getattr(fn, '__name__', fn)} упал: {type(e).__name__}: {e}")
            continue
        if text and str(text).strip():
            out.append(str(text).strip("\n"))
    return out


def reset() -> None:
    """Забыть загруженные модули (для тестов): следующий load() импортирует заново.
    Их функции убираются из реестров базового слоя (PRE_CHOOSE, PRE_ANSWER, …)."""
    global _loaded, _loaded_dir
    stems = list(_loaded or {})
    for stem in stems:
        sys.modules.pop(f"{PACKAGE}.{stem}", None)
    _purge(stems)
    _loaded = None
    _loaded_dir = None
    errors.clear()
