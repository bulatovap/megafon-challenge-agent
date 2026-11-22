---
name: controls
description: Шаги 8–9 — контроль: механическая проверка, затем чтение outside-reader без переписки. Перед утверждением решения и после крупных правок.
---

# Шаги 8–9/11 · Механическая проверка и независимое чтение

Перед первым действием прочитай `knowledge/references/04_strong_objections.md`
и `knowledge/references/19_epistemics_phrasing.md`.

1. `python3 -m tools.bulochka status`; `python3 -m tools.bulochka journal begin --step review` (запомни J###) — при `skip_done` не повторяй.
2. Шаг 8: `python3 -m tools.bulochka verify --quotes --numbers --json` и `python3 -m tools.bulochka check --json`.
   Исправь каждую ошибку по сути: нет цитаты — понизь до гипотезы; число без тега — поставь тег или убери число.
   Ошибку не разбавляют: балл не важен, пока есть хоть одна ошибка.
3. Шаг 9: запусти `outside-reader` с одним указанием — slug проекта. Он восстановит решение по файлам и вернёт разрывы.
4. Разрывы, которые меняют рекомендацию, — исправь; остальные запиши. Возражение, которого раньше не было, —
   `objection add`.
5. Итераций исправления не больше двух. После второй, если разрыв остался, — сам вынеси ruling:
   что решили — почему — цена ошибки. Не крути третий круг.
6. Сохрани `projects/<slug>/report/60_controls.md`: результаты `verify` и `check` (числа), разрывы outside-reader
   и что с ними сделано, rulings, 2–3 вопроса человеку «что проверить перед решением».
7. `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/60_controls.md`.

▶ Далее: `/approval` — утверждение решения человеком (ворота 3).
