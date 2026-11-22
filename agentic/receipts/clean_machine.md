# Чек чистой машины

22.11.2026. Закоммиченная папка продукта выгружена (`git archive`) в путь с кириллицей и пробелом: 226 файлов,
без `pip`, без ключей, только стандартная библиотека Python. Claude Code 2.1.330.

| Проверка | Итог |
|---|---|
| `/usr/bin/python3 -m unittest discover -s tests` (3.9.6) | 271 OK, 15 с |
| `python3 -m unittest discover -s tests` (3.14.6) | 271 OK, 16 с |
| `python3 -m tools.bulochka doctor` | «Среда в порядке» |
| `python3 -m tools.bulochka --project mf-family-devices check` | 82/100, 1 ошибка E19 — след исправленного дефекта ценника (`megafon_demo.md`) |

Python 3.14 строже 3.9 к справке `argparse`: знак `%` в тексте справки ронял CLI — нашли тестами на двух
интерпретаторах до релиза и экранировали.
