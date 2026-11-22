---
name: reforecast
description: Шаг 11 — новые данные или срок: пересчёт, что затронуто, нужен ли пересмотр; пересмотр — через человека.
disable-model-invocation: true
---

# Шаг 11/11 · Сопровождение и пересмотр

Перед первым действием прочитай `knowledge/references/17_decision_record.md` (условия пересмотра)
и `knowledge/references/13_evidence_ledger.md` (замена и отзыв записей).

1. `python3 -m tools.bulochka status`; `python3 -m tools.bulochka journal begin --step revise` (запомни J###);
   `python3 -m tools.bulochka decision due`; `python3 -m tools.bulochka freshness`.
   Покажи человеку: какие условия пересмотра наступили, какие источники устарели.
2. Нужен новый сбор из сети — сними заморозку: `python3 -m tools.bulochka freeze --off --reason "…"`.
   Новые данные регистрируй как обычно: `source fetch --url` или `source add --file`, затем `claim add` с цитатой. Устаревший источник —
   `source refresh S###`.
3. Если новое утверждение заменяет старое: `claim supersede C### --by C###` и
   `python3 -m tools.bulochka verify --impact C###` — список задетых выводов, вариантов и решений.
4. Отвергнутые варианты, у которых сработало условие возврата, покажи (`python3 -m tools.bulochka notdoing`).
   Сам не возвращаешь: вернуть — только по ответу человека (шаг 6).
5. Предложение ревизии для каждого задетого D### — один из трёх исходов:
   подтвердить (предпосылка устояла), скорректировать (меняется часть стратегии), заменить решение.
   Покажи `decision diff D###`: что меняется и из-за каких C###/R###.
6. Утверждённое решение сам не правишь. Спроси одним вопросом: `ask --kind approve_decision --question "Что делаем с D###?" --options "Подтвердить" "Скорректировать" "Заменить — вернуть O###" --flips --why "…" --default "оставляем как есть до ответа"`,
   ответ — `answer G### --answer "<его слова>"`. Ответ называет вариант — `option revive O### --gate G###` с этими же
   воротами (второй раз не спрашиваешь).
7. «Скорректировать» или «заменить» → черновик-преемник `decision add --supersedes D### --proposed …`
   (старое решение в силе до выбора), затем `/alternatives` и дальше по шагам — повторяются только задетые разделы.
8. Сохрани `projects/<slug>/report/80_reforecast.md` (что наступило, что задето, предложение, ответ человека);
   `python3 -m tools.bulochka journal commit J### --output projects/<slug>/report/80_reforecast.md`; `python3 -m tools.bulochka pause`.

▶ Далее: при замене — `/alternatives`; иначе ждём следующей даты из `bulochka decision due`.
