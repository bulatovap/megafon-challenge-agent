"""verify_quote: поиск дословной цитаты в тексте источника с сохранением offset.

Нормализация (и для текста, и для цитаты): NFC, все пробелы → один пробел, кавычки «»“”„ → ",
’‘ → ', тире и минусы → -, … → ..., ё → е, мягкие переносы и нулевые пробелы убраны.
Цитата может содержать пропуски «...» или «[...]» — части ищутся по порядку.
Offset — символы в исходном файле sources/S###.md: [start, end).
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

_DQ = "\"«»“”„‟″"
_SQ = "'‚‘’‛′`"
_DASH = "‐‑‒–—―−"
_DROP = "­​‌‍﻿"


def _norm_char(ch: str) -> str:
    if ch in _DROP:
        return ""
    if ch in _DQ:
        return '"'
    if ch in _SQ:
        return "'"
    if ch in _DASH:
        return "-"
    if ch == "…":
        return "..."
    if ch == "ё":
        return "е"
    if ch == "Ё":
        return "Е"
    if ch.isspace():
        return " "
    return ch


def normalize_map(text: str, fold: bool = False) -> Tuple[str, List[int], List[int]]:
    """Нормализованная строка + для каждого её символа начало и конец кластера в исходнике."""
    out: List[str] = []
    starts: List[int] = []
    ends: List[int] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        cluster = unicodedata.normalize("NFC", text[i:j])
        for ch in cluster:
            rep = _norm_char(ch)
            if fold:
                low = rep.lower()
                rep = low if len(low) == len(rep) else rep
            for r in rep:
                if r == " " and (not out or out[-1] == " "):
                    continue  # схлопываем пробелы и убираем ведущий
                out.append(r)
                starts.append(i)
                ends.append(j)
        i = j
    return "".join(out), starts, ends


def normalize(text: str, fold: bool = False) -> str:
    return normalize_map(text, fold)[0]


def _quote_parts(quote: str, fold: bool) -> List[str]:
    q = normalize(quote, fold).strip()
    q = q.strip('"\'').strip()
    parts = [p.strip(" \"'") for p in re.split(r"\s*(?:\[\s*\.\.\.\s*\]|\(\s*\.\.\.\s*\)|\.\.\.)\s*", q)]
    return [p for p in parts if p]


def verify_quote(quote: str, text: str) -> Optional[Tuple[int, int]]:
    """Найти цитату. Возвращает [start, end) в исходном тексте или None."""
    if not quote or not text:
        return None
    for fold in (False, True):
        parts = _quote_parts(quote, fold)
        if not parts:
            return None
        norm, starts, ends = normalize_map(text, fold)
        pos, first, last = 0, None, None
        ok = True
        for part in parts:
            k = norm.find(part, pos)
            if k < 0:
                ok = False
                break
            if first is None:
                first = k
            last = k + len(part) - 1
            pos = k + len(part)
        if ok and first is not None and last is not None:
            return starts[first], ends[last]
    return None
