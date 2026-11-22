---
name: sourcing
description: Шаг 3 — сбор фактов в пределах бюджета шага: источники с дословной цитатой, остановка при насыщении. И когда человек приносит новые файлы.
---

# Шаг 3/11 · Сбор материалов

Перед первым действием прочитай `knowledge/references/13_evidence_ledger.md` и `projects/<slug>/cost_plan.md`.

1. `python3 -m tools.bulochka status`. Проверь, что ворота 1 (brief_approval) пройдены; если нет — вернись к `/cost-plan`.
   `python3 -m tools.bulochka journal begin --step research` (запомни J###) — при `skip_done` не повторяй.
2. `python3 -m tools.bulochka source list` — что уже загружено (офлайн-корпус, файлы человека). Это читаешь первым;
   повторно не загружаешь.
3. Методика, стандарт, учебник (CONSORT, руководство по A/B-тестам) — `--kind method`: срока годности у них нет,
   отказа STALE_SOURCE не будет.
   Файлы человека регистрируй сам: `python3 -m tools.bulochka source add --url file://<путь> --title "…" --publisher "…" --file <путь> --kind company`.
   Таблицы считай кодом: когорты — `python3 scripts/calc.py cohort --csv <файл> --json` (разрез по сегментам,
   плато, пропуски); результат — `claim add --type inference --calc --plan-hash … --from C###,…` (готовую строку
   печатает calc.py). Пропуски в данных не заполняешь — пишешь их в пробелы.
4. Веб — только если корпуса не хватает по вопросу из топа плана. Запусти субагентов `lean-researcher` — по одному на
   вопрос, не больше 3: slug, один вопрос Q#, switch point, где искать, бюджет (до 10 запросов). Весь план не передавай.
   Страницы сохраняются только `source fetch --url` (WebFetch отдаёт пересказ — цитаты по нему не сверить).
5. Каждый вернёт `## RESEARCH COMPLETE` или `## BLOCKED` со списком S### и кандидатами с цитатами.
   Для каждого: `python3 -m tools.bulochka claim add --text "…" --type fact --source S### --quote "…" [--link D###] [--metric <id> --period … --denominator …]`.
   Отказ `NO_SOURCE` — цитаты на странице нет: `--type assumption` или другой источник.
   Отказ `INCOMPARABLE_METRICS` — у метрики разные окно или знаменатель: передай человеку, в одну таблицу не ставь.
6. Исключительное утверждение держится на двух независимых источниках (разные издатели) — иначе это пробел.
7. Пробел записывай структурно: вопрос, что пробовали, почему не вышло, как закрыть, цена закрытия.
8. Остановись, когда по каждому вопросу топа есть ответ или честный пробел, или два поиска подряд не дали нового.
9. Гипотезы о мотивах аудитории — можно `mock-customer`; ответ в `projects/<slug>/interviews/synthetic/<n>.md`,
   `source add … --kind synthetic`, утверждения только `--type synthetic`, рядом план реальных интервью.
10. `python3 -m tools.bulochka verify --quotes --numbers`. Сохрани `projects/<slug>/report/10_sourcing.md`:
    сколько S###, C###, сколько прошло проверку цитат, ответы по вопросам плана со ссылками, пробелы.
11. `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/10_sourcing.md`.

▶ Далее: `/baseline` — картина ситуации: что известно, откуда и чего мы не знаем.
