# Booking bot + mini app

## Перед первым запуском
1. **Если хочешь сохранить команды старого бота** — пришли мне его `bot.py`, я сам вставлю их в `main.py`. Без этого файл рабочий, но со старыми командами (программы/КБЖУ по-старому, если они были) бот пока не умеет — только то, что добавлено здесь.
2. `DB_PATH` — новый файл (`booking.db`), не текущая база клиентов — они не связаны.

## Переменные окружения
| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен твоего бота |
| `TRAINER_TG_ID` | твой Telegram id (узнать через `@userinfobot`) |
| `MINIAPP_URL` | публичный домен сервиса (см. ниже) |
| `DB_PATH` | `/data/booking.db` — must match the Volume mount path (see deploy steps) |

## Деплой на Railway — только терминал, без панели
Актуальные команды CLI (проверено по docs.railway.com):

```bash
npm install -g @railway/cli
cd booking-repo
railway up -y                              # вход в аккаунт + создание проекта + деплой одной командой
railway variable set BOT_TOKEN=твой_токен
railway variable set TRAINER_TG_ID=твой_id
railway variable set DB_PATH=/data/booking.db
railway volume add -m /data                # том, чтобы база не стиралась при редеплое
railway domain                             # выдаст https://...up.railway.app — скопировать
railway variable set MINIAPP_URL=https://то-что-выдал-domain
railway up                                 # передеплой, чтобы подхватились переменные и том
```

Ссылка: https://railway.com — там же дашборд, если что-то захочешь посмотреть глазами (Deployments → Logs).

## Альтернатива — через GitHub + дашборд
Если удобнее в браузере: `git init`, коммит, пуш на GitHub, затем на railway.com — New Project → Deploy from GitHub repo. Переменные и Volume — там же, во вкладках Variables и Settings → Volumes.

## Проверка после деплоя
- В Telegram открой бота → должна появиться кнопка меню «Запись»
- `/clients` — пусто, пока никто не открывал мини-эп
- Открой мини-эп с любого аккаунта → `/clients` должен показать этот id
- `/add_slot 2026-09-20 18:00 60 2` → слот должен появиться в мини-эпе
- `/set_program <id> текст программы` → в мини-эпе под этим id, вкладка «Моя программа», должен показать текст
- `/set_kbju <id> 2400 180 70 250`
- Забронировать слот → должно прийти уведомление тебе в бота с ростером
