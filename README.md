# DISCIPLINE Bot

Telegram-бот тренера: программа, КБЖУ, тренировки, прогресс, вода, привычки,
запись на тренировку, проверка техники и Mini App.

## Архитектура

Один Railway service запускает:
- Telegram polling
- FastAPI + Mini App
- PostgreSQL

Не запускай второй процесс polling с тем же BOT_TOKEN.

## Переменные Railway

- `BOT_TOKEN` — токен BotFather
- `TRAINER_TG_ID` — Telegram ID тренера
- `DATABASE_URL` — Reference на PostgreSQL
- `MINIAPP_URL` — публичный URL Railway-сервиса
- `TRAINER_TZ` — по умолчанию `Europe/Minsk`
- `ANTHROPIC_API_KEY` — необязательно; требуется только для оценки еды по фото
- `PORT` — Railway передаёт автоматически

## Локальная проверка

```bash
python -m compileall .
pytest -q
```

Для полноценного запуска нужны переменные окружения и PostgreSQL.

## Основные команды клиента

`/start`, `/profile`, `/plan`, `/nutrition`, `/workout`, `/report`,
`/progress`, `/measurements`, `/photo`, `/water`, `/habits`, `/bookings`,
`/coach`, `/help`

Вода: `/water_add 250`

Рабочий вес:
`/log_lift жим 80 8 4`

## Команды тренера

`/clients`
`/set_program <telegram_id> <текст>`
`/set_kbju <telegram_id> <калории> <белки> <жиры> <углеводы>`
`/add_slot <ГГГГ-ММ-ДД> <ЧЧ:ММ> <минуты> <места>`
`/roster <ГГГГ-ММ-ДД>`
`/set_weekly пн,вт 07:00,18:00`
`/weekly_schedule`
`/clear_weekly пн`
`/pending_reviews`
`/progress <telegram_id> <упражнение>`

## Важное

Оценка еды по фотографии является приблизительной и зависит от `ANTHROPIC_API_KEY`.
Фото прогресса пока не сохраняются в object storage. Для production-хранилища фото
нужен отдельный S3-compatible bucket.

Перед первым production deploy обязательно проверить реальный Telegram update,
PostgreSQL connection и Mini App через Railway logs.
