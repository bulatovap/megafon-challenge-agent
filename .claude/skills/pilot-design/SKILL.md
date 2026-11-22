---
name: pilot-design
description: Шаг 7 — дизайн пилота: ожидаемый эффект, контроль, срок, нежелательные последствия и сколько стоит наблюдение.
---

# Шаг 7/11 · План проверки

Перед первым действием прочитай `knowledge/references/14_verification_plan.md`
и шаблон `knowledge/templates/pilot_design.md`.
Строку «Цена этапа» в начале документа заполни перед commit из `python3 -m tools.bulochka cost --step validation-plan`.

1. `python3 -m tools.bulochka status`; `python3 -m tools.bulochka journal begin --step validation-plan` (запомни J###) — при `skip_done` не повторяй.
2. Возьми из `report/40_playbook.md` самую рискованную предпосылку и гипотезы H1…Hn (`claim` с типом
   `assumption` или `forecast`). Каждой гипотезе — свой блок шаблона.
3. Ожидаемое изменение: одна главная метрика, направление, минимальный эффект, ради которого стоит выкатывать,
   и связь со switch point из `cost_plan.md`.
4. База сравнения: A/B, holdout, исторический baseline или ступенчатый запуск — и слабость выбранного.
5. MDE, выборку и срок считай кодом: `python3 scripts/calc.py mde --base <доля> --lift <эффект> [--lift-type abs] --daily <поток/день> --json`;
   входы — из реестра, данных продакта (`calc.py cohort`) или явные допущения. Покажи входы, `n_per_variant`, срок
   и `plan_hash`; результат запиши строкой `claim_hint` из вывода. Минимум 2 полные недели.
   Проверка сплита после старта — `calc.py srm`. Прочая арифметика (поток семей в неделю, доля охвата) —
   `calc.py schet "…" --var имя=число --json`.
6. Нежелательные последствия: каннибализация, обращения в поддержку, отток старых клиентов — с порогами автостопа.
7. Критерии решения до старта: при каком результате масштабируем, дорабатываем, закрываем.
8. Где нужны реальные интервью — число и гайд; синтетика спрос не доказывает.
9. Прогнозы — диапазоном, с горизонтом и базовой ставкой (`claim add --type forecast`), без точечных вероятностей.
10. Сохрани `projects/<slug>/report/50_pilot_design.md`; `python3 -m tools.bulochka check`;
    `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/50_pilot_design.md`.

▶ Далее: `/controls` — механическая проверка и независимое чтение перед решением.
