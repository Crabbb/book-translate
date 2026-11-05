# Шаг 0. Бриф

Перед началом запиши `bible/passport.yaml`. Это короткое описание задачи перевода, а не пересказ книги.

```yaml
passport:
  source: {audience: "...", purpose: "...", genre: technical_book, domain: [cloud, llm]}
  target: {audience: "...", purpose: "...", locale: ru-RU}
  function: instrumental       # читатель перевода должен суметь то же, что читатель оригинала
  content_correspondence: full
  address: {you: "вы", author_we: "мы", impersonal_policy: "..."}
  modal_map: {must: "должен, нужно", should: "следует, стоит", may: "может, возможно", can: "может, можно"}
  code_policy: {comments: keep, output: keep, prompts_in_code: keep}
  typography: {quotes: "«»", dash: "—", percent: "16 %", decimal: ",", units: "16 ГБ"}
  figures: {raster_text: caption}
  provisional: []              # поля, которые владелец не подтвердил
rules: []                      # см. bible.md
```

## Вопросы владельцу

Задай их одним сообщением, только то, что меняет результат, и сразу предложи значение по умолчанию:

- Аудитория перевода, если она отличается от оригинала.
- Комментарии в коде: по умолчанию не переводить.
- Промты в тексте и коде: по умолчанию оригинал и перевод. Код не меняется, перевод отдельным блоком под ним.
- Реклама издателя, страница авторских прав: по умолчанию переводить, реквизиты без изменений.

Не жди ответа, если можно идти дальше: занеси поле в `provisional` и работай со значением по умолчанию.
