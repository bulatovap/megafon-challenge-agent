"""Нормализация чисел в русском тексте и проверка «число есть на странице».

Понимает: неразрывные и тонкие пробелы, пробел как разделитель тысяч, запятую как
десятичный разделитель, %, п.п., тыс/млн/млрд/трлн, ₽/руб., $/долл.
Результат — список {raw, value, unit, scale}: value уже умножен на scale,
проценты переведены в долю (41% → 0.41, unit=share).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

SPACES = "           "

SCALES = [
    (r"трлн\.?|триллион(?:ов|а)?", 1e12),
    (r"млрд\.?|миллиард(?:ов|а)?", 1e9),
    (r"млн\.?|миллион(?:ов|а)?", 1e6),
    (r"тыс\.?|тысяч[аи]?", 1e3),
]

_NUM = r"(?:(?P<pre>\$|€|₽)\s?)?(?<![\w.,])(?P<num>\d{1,3}(?:[ ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
_SCALE = r"(?:\s?(?P<scale>" + "|".join(p for p, _ in SCALES) + r"))?"
_UNIT = (r"(?:\s?(?P<unit>%|процент(?:ов|а)?|п\.\s?п\.?|пп\b|₽|руб(?:\.|лей|ля|ль)?|р\.|\$|долл(?:\.|аров|ара)?|"
         r"USD|RUB))?")
_RE = re.compile(_NUM + _SCALE + _UNIT, re.IGNORECASE)
_YEAR_AFTER = re.compile(r"^\s?(?:год|года|году|г\.|гг\.)", re.IGNORECASE)


def clean(text: str) -> str:
    """Неразрывные/тонкие пробелы → обычный пробел, минус-тире → дефис."""
    for ch in SPACES:
        text = text.replace(ch, " ")
    return text.replace("−", "-")


def _scale_of(word: Optional[str]) -> float:
    if not word:
        return 1.0
    w = word.lower()
    for pat, mult in SCALES:
        if re.fullmatch(pat, w, re.IGNORECASE):
            return mult
    return 1.0


def _unit_of(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.lower().replace(" ", "")
    if w == "%" or w.startswith("процент"):
        return "share"
    if w.startswith("п.п") or w == "пп":
        return "pp"
    if w in ("₽", "р.", "rub") or w.startswith("руб"):
        return "RUB"
    if w in ("$", "usd") or w.startswith("долл"):
        return "USD"
    return None


_CSV_LINE = re.compile(r"^[^\n]*?,\S[^\n]*?,\S[^\n]*?,\S")
# «171,8 млн», «92,7%», «2,5 млн ₽»: десятичная запятая и единица после — это текст, а не поле CSV
_DECIMAL_WITH_UNIT = re.compile(r"\d,\d+\s?(?:" + "|".join(p for p, _ in SCALES)
                                + r"|%|процент|п\.\s?п|₽|руб|долл|\$)", re.IGNORECASE)


def _is_csv_line(line: str) -> bool:
    return (line.count(",") >= 3 and ", " not in line and bool(_CSV_LINE.match(line))
            and not _DECIMAL_WITH_UNIT.search(line))


def _csv_safe(text: str) -> str:
    """Строка CSV («2026-03,промо,12400,2121,…»): запятая — разделитель полей, а не десятичная.
    Признак: ≥3 запятых, ни одной «, » с пробелом и нет десятичного числа с единицей («171,8 млн», «92,7%»).
    Признак берётся по полной строке текста (не по обрезку окна, см. number_on_page).
    Замена той же длины — смещения не плывут."""
    out = []
    for line in text.split("\n"):
        if _is_csv_line(line):
            line = line.replace(",", ";")
        out.append(line)
    return "\n".join(out)


def extract(text: str, csv_safe: bool = True) -> List[Dict[str, Any]]:
    """Все числа текста в нормализованном виде. csv_safe=False — текст уже прошёл _csv_safe по полным строкам."""
    text = clean(text or "")
    if csv_safe:
        text = _csv_safe(text)
    out: List[Dict[str, Any]] = []
    for m in _RE.finditer(text):
        raw_num = m.group("num")
        num = float(raw_num.replace(" ", "").replace(",", "."))
        scale = _scale_of(m.group("scale"))
        unit = _unit_of(m.group("unit")) or _unit_of(m.group("pre"))
        value = num * scale
        if unit == "share":
            value = num / 100.0 * scale
        elif unit is None:
            tail = text[m.end():m.end() + 6]
            if scale == 1.0 and raw_num.isdigit() and 1900 <= num <= 2100 and _YEAR_AFTER.match(tail):
                unit = "year"
            else:
                unit = "count"
        value = round(value, 10)
        if value.is_integer():
            value = int(value)
        out.append({"raw": m.group(0).strip(), "value": value, "unit": unit, "scale": int(scale) if scale >= 1 else scale})
    return out


def _close(a: float, b: float, rel: float) -> bool:
    if a == b:
        return True
    d = max(abs(a), abs(b))
    return d > 0 and abs(a - b) / d <= rel


def _compatible(u1: str, u2: str) -> bool:
    return u1 == u2 or "count" in (u1, u2)


def match_one(n: Dict[str, Any], page: List[Dict[str, Any]]) -> str:
    """pass — то же значение и единица; needs_manual — похоже (другая единица/округление); fail — нет."""
    near = False
    for p in page:
        if _close(float(n["value"]), float(p["value"]), 1e-9):
            if n["unit"] == p["unit"]:
                return "pass"
            near = True
        elif _compatible(n["unit"], p["unit"]) and _close(float(n["value"]), float(p["value"]), 0.02):
            near = True
        elif n["unit"] == "share" and p["unit"] == "count" and _close(float(n["value"]) * 100, float(p["value"]), 1e-9):
            near = True  # «41%» против «41» на странице
    return "needs_manual" if near else "fail"


DEFAULT_WINDOW = 400  # символов по обе стороны цитаты (эксперимент №6: окно 300–500)


def number_on_page(claim_numbers: List[Dict[str, Any]], page_text: Optional[str],
                   offset: Optional[List[int]] = None, window: int = DEFAULT_WINDOW) -> Optional[str]:
    """Сводный статус для claim: None — чисел нет; иначе pass / fail / needs_manual.

    Число ищется в сохранённом тексте источника (sources/S###.md) в окне ±window вокруг quote_offset,
    а не по всей странице: короткое «15%» иначе совпадает случайно. Нет цитаты — needs_manual.
    """
    nums = [n for n in claim_numbers if n.get("unit") != "year"]
    if not nums:
        return None
    if not page_text or not offset:
        return "needs_manual"
    lo = max(0, int(offset[0]) - window)
    hi = min(len(page_text), int(offset[1]) + window)
    # CSV определяется по полным строкам страницы, а не по обрезку окна: у обрезанной крайней строки
    # «≥3 запятых без «, »» ложно срабатывает на русском тексте с десятичными запятыми.
    # clean и _csv_safe не меняют длину, поэтому смещения окна те же.
    safe = _csv_safe(clean(page_text))
    page = extract(safe[lo:hi], csv_safe=False)
    results = [match_one(n, page) for n in nums]
    if all(r == "pass" for r in results):
        return "pass"
    if any(r == "fail" for r in results):
        return "fail"
    return "needs_manual"
