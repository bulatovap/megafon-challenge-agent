#!/usr/bin/env python3
"""Сегмент строки статуса «≈N ₽ подп.» (≈ рублей из стоимости подписки) — вторым номером после «% недели»
(слот базового слоя для ext/statusline_*.py). Коротко, потому что строка ≤100 символов и базовый слой первым режет сегменты:
типичная строка базового слоя — 85 символов, «≈37 ₽ из подписки» в неё уже не входит. Полная подпись — в `bulochka cost`,
смете и ценнике.

Рубли берутся из state/cost.json (`rub_equiv_total`: цена подписки за неделю × Δ% недельного лимита), который
statusline базового слоя пишет до вызова сегментов. Нет замера доли недели — сегмента нет (рубли без доли недели не считаем).
Никогда не падает: при сбое ничего не печатает.
"""
from __future__ import annotations

import sys

import _econ_hook as h


def segment():
    s = h.slug()
    if not s:
        return ""
    from tools.bulochka import cost
    rub = cost.load_cost(s).get("rub_equiv_total")
    if not isinstance(rub, (int, float)) or rub <= 0:
        return ""
    return f"≈{rub:.0f} ₽ подп."


def main():
    try:
        h.read_stdin()
        line = segment()
        if line:
            print(line)
    except Exception:  # noqa: BLE001 — строка статуса не должна страдать от сегмента
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
