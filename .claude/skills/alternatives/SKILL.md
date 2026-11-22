---
name: alternatives
description: Шаг 5 — альтернативы с рублёвой строкой, включая «оставить как есть» и «закрыть», возражения от objection-finder. Ворота 2: выбирает человек.
disable-model-invocation: true
---

# Шаг 5/11 · Варианты и возражения — ворота 2

Перед первым действием прочитай `knowledge/references/03_options_map.md`, `knowledge/references/04_strong_objections.md`
и шаблон `knowledge/templates/alternatives.md`.
Строку «Цена этапа» в начале документа заполни перед commit из `python3 -m tools.bulochka cost --step options`.

1. `python3 -m tools.bulochka status`; `python3 -m tools.bulochka journal begin --step options` (запомни J###) — при `skip_done` не повторяй.
2. Три перспективы по очереди, каждая со своим мандатом, на одних и тех же C###:
   «сохранить» — лучший способ ничего не ломать; «закрыть» — честное основание выйти (экономика, commodity, не crux);
   «расти» — 1–3 пути, которые различаются аудиторией, моделью дохода, каналом или ставкой, а не степенью.
3. Модератор: найди записи реестра, на которые не опёрся ни один вариант. Меняют ли они что-то? Если да — предложи
   вернуться в `/sourcing` с одним вопросом.
4. Запиши варианты: `python3 -m tools.bulochka option add --decision D### --title "…" --kind keep|close|change [--distinct-by audience|revenue_model|channel|bet]`.
   Обязательно один `keep` и один `close`; у `change` — `--distinct-by`. Лидеру: `option set O### --leader --flip-condition "…"`.
5. Запусти `objection-finder` по всем вариантам. Его R### запиши: `objection add --target O### --text "…" --severity high|medium|low [--evidence C###]`.
6. Каждую оценку варианта запиши и в реестр, со ссылками на утверждения: `option set O### --scores
   '{"<критерий>":{"score":3,"tag":"Гипотеза","claim_ids":["C###"]}}'` (и `--money-line` с claim_ids для денег).
   Деньги и эффекты вариантов считаешь `python3 scripts/calc.py schet "…" --var имя=число --json`, а не в уме.
   Утверждения, привязанные только к D###, вариант «не видит»: на чём он держится, CLI не покажет.
   Заполни `projects/<slug>/report/30_alternatives.md`: switch points, критерии с весами, варианты, проверка различимости,
   таблица «вариант × критерий» — каждая ячейка с тегом. Для лидера — условие, при котором выбор меняется.
7. `python3 -m tools.bulochka check` — нет ошибок по числу вариантов, keep, close и возражениям.
8. Ворота 2: `ask --kind choose_option --question "Какой вариант берём?" --options "O001 …" "O002 …" "O003 …" --flips --why "…" --default "без выбора дальше не идём"`.
   Покажи таблицу, рекомендацию, сильнейшее возражение к ней и что её перевернёт. Выбирает человек.
9. Ответ человека — `answer G### --answer "<его слова дословно>"` (если его уже записали, не перезаписывай). Затем
   `decision choose D### --option O### --gate G### --revise-if '[{"indicator":"…","revise_when":"≥ 50%","source":"S###","check_by":"YYYY-MM-DD","assumption_id":"C###"}]'`
   (без `--revise-if` — ошибка E20). `revise_when` — когда пересматривать; CLI ответит, как понял:
   «держится, пока …; пересмотр, когда …» — сверь, что смысл не перевёрнут.
   Выбор не тот, что ты рекомендовал, — ставишь выбранный человеком.
10. Каждый невыбранный вариант — сразу отклоняешь: `option reject O### --killed-by R###,C### --revive-if '[{"assumption_id":"C###","threshold":"<0,30"}]'`
   (killed_by — что его убило; revive_if — при каком значении вернуть, здесь порог = условие возврата).
   Пока хоть один вариант открыт, `decision lock` откажет (W14), а после решения нечего будет вернуть на стол.
11. `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/30_alternatives.md`.

▶ Далее: `/playbook` — продуктовая и коммуникационная стратегия выбранного варианта.
