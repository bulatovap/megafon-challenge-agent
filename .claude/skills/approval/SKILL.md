---
name: approval
description: Шаг 10 — утверждение решения; после него — ценник и первый экран для руководства. Ворота 3: утверждает человек.
disable-model-invocation: true
---

# Шаг 10/11 · Решение — ворота 3

Перед первым действием прочитай `knowledge/references/17_decision_record.md`.

1. `python3 -m tools.bulochka status`; `python3 -m tools.bulochka journal begin --step decide` (запомни J###) — при `skip_done` не повторяй.
   Если в `check` есть ошибки — вернись к `/controls`.
2. Заполни `projects/<slug>/report/70_approval.md` по `17_decision_record.md`: вопрос; выбранный вариант
   и отвергнутые с причинами; тип двери (односторонняя / двусторонняя); владелец (роль); метрика и дата
   первого чтения; предпосылки с типом (факт / допущение) и минимальный грейд оснований; прогноз диапазоном;
   условия пересмотра: показатель, порог, источник, дата проверки, какое допущение он проверяет.
3. Если доказательства слабые (грейд C и ниже), а рекомендация сильная — напиши почему.
4. Ворота 3: `ask --kind approve_decision --question "Утверждаете решение D###?" --options "Утвердить" "Утвердить с правкой" "Вернуться к вариантам" --flips --why "…" --default "без утверждения решение остаётся черновиком"`.
   Покажи ADR в 10 строках и два вопроса «что проверить перед решением» из `60_controls.md`.
5. Ответ — `answer G### --answer "<его слова дословно>"`. Только при явном «утвердить»: `python3 -m tools.bulochka decision lock D### --gate G###`.
   Lock ответил W14 (открытые варианты) — отклони каждый (`option reject … --killed-by … --revive-if …`) и повтори lock с теми же воротами.
   «Вернуться к вариантам» → `/alternatives`; правка → внести, перепроверить `check`, спросить снова.
6. Документ для комитета: `python3 -m tools.bulochka export --one-pager`.
7. `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/70_approval.md`; `python3 -m tools.bulochka pause`.
8. Скажи человеку, сколько это стоило: `python3 -m tools.bulochka cost` — токены, минуты человека, доля лимита.

▶ Далее: `/reforecast` — когда наступит дата проверки или придут новые данные; `bulochka decision due` покажет, что пора.
