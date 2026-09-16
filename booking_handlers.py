"""
Core bot commands: /start, slot management, client roster, program/КБЖУ
assignment. Wire into your Application with:

    from booking_handlers import register_booking_handlers, set_booking_menu_button
    register_booking_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    await set_booking_menu_button(application, miniapp_url=MINIAPP_URL)  # once at startup

Trainer commands this adds:
    /clients                          - list everyone who's opened the mini app, with their Telegram id
    /set_program <id> <текст>         - assign/update a client's program
    /set_kbju <id> <cal> <p> <f> <c>  - assign/update a client's КБЖУ targets
    /add_slot <date> <time> <min> <cap> - open a training slot
    /roster <date>                    - who's booked into each slot that day

Client commands this adds:
    /start                            - greeting + pointer to the mini app
"""

from datetime import datetime

from telegram import Update, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes

from booking_api import get_conn


def register_booking_handlers(application: Application, trainer_tg_id: int):
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Чтобы записаться на тренировку и посмотреть свою программу — "
            "открой кнопку «Запись» внизу экрана."
        )

    async def add_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        try:
            date, time_, duration, capacity = (
                context.args[0], context.args[1], int(context.args[2]), int(context.args[3])
            )
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Формат: /add_slot ГГГГ-ММ-ДД ЧЧ:ММ длительность мест\n"
                "Пример (группа на 4): /add_slot 2026-09-20 18:00 60 4"
            )
            return
        conn = get_conn()
        slot_id = f"{date}_{time_}"
        conn.execute(
            "INSERT INTO slots (id, date, time, duration, capacity) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET date = EXCLUDED.date, time = EXCLUDED.time, "
            "duration = EXCLUDED.duration, capacity = EXCLUDED.capacity",
            (slot_id, date, time_, duration, capacity),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Слот добавлен: {date} {time_}, мест: {capacity}")

    async def roster(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        if not context.args:
            await update.message.reply_text("Формат: /roster ГГГГ-ММ-ДД")
            return
        target_date = context.args[0]
        conn = get_conn()
        slots = conn.execute(
            "SELECT * FROM slots WHERE date = %s ORDER BY time", (target_date,)
        ).fetchall()
        if not slots:
            await update.message.reply_text(f"На {target_date} слотов нет.")
            conn.close()
            return
        lines = []
        for s in slots:
            names = conn.execute(
                "SELECT client_name FROM bookings WHERE slot_id = %s AND status = 'active'",
                (s["id"],),
            ).fetchall()
            names_str = ", ".join(n["client_name"] for n in names) if names else "пусто"
            lines.append(f"{s['time']} ({len(names)}/{s['capacity']}): {names_str}")
        conn.close()
        await update.message.reply_text(f"Запись на {target_date}:\n" + "\n".join(lines))

    async def clients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lists everyone who has ever opened the mini app, with their
        Telegram id — this is where you get the ID for /set_program."""
        if update.effective_user.id != trainer_tg_id:
            return
        conn = get_conn()
        rows = conn.execute(
            "SELECT telegram_id, first_name, username FROM known_users ORDER BY first_seen DESC"
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Пока никто не открывал мини-эп.")
            return
        lines = [
            f"{r['telegram_id']} — {r['first_name'] or ''}" + (f" (@{r['username']})" if r["username"] else "")
            for r in rows
        ]
        await update.message.reply_text("Клиенты:\n" + "\n".join(lines))

    async def set_program(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "Формат: /set_program <telegram_id> <текст программы>\n"
                "ID клиента бери из /clients"
            )
            return
        client_id = int(context.args[0])
        text = " ".join(context.args[1:])
        conn = get_conn()
        conn.execute(
            "INSERT INTO client_profiles (telegram_id, program_text, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET program_text = EXCLUDED.program_text, "
            "updated_at = EXCLUDED.updated_at",
            (client_id, text, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("Программа сохранена.")

    async def set_kbju(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        try:
            client_id, calories, protein, fat, carbs = (
                int(context.args[0]), int(context.args[1]), int(context.args[2]),
                int(context.args[3]), int(context.args[4]),
            )
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Формат: /set_kbju <telegram_id> <калории> <белки> <жиры> <угли>\n"
                "Пример: /set_kbju 123456789 2400 180 70 250"
            )
            return
        conn = get_conn()
        conn.execute(
            "INSERT INTO client_profiles (telegram_id, calories, protein, fat, carbs, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET calories = EXCLUDED.calories, "
            "protein = EXCLUDED.protein, fat = EXCLUDED.fat, carbs = EXCLUDED.carbs, "
            "updated_at = EXCLUDED.updated_at",
            (client_id, calories, protein, fat, carbs, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("КБЖУ сохранены.")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_slot", add_slot))
    application.add_handler(CommandHandler("roster", roster))
    application.add_handler(CommandHandler("clients", clients_cmd))
    application.add_handler(CommandHandler("set_program", set_program))
    application.add_handler(CommandHandler("set_kbju", set_kbju))


async def set_booking_menu_button(application: Application, miniapp_url: str):
    """Persistent Menu Button next to the chat's text input. Independent
    of /start — shows up regardless of whether /start has ever been sent."""
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Запись", web_app=WebAppInfo(url=miniapp_url))
    )
