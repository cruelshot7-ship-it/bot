from datetime import datetime

from telegram import Update, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes

from booking_api import get_conn


def _trainer(update, trainer_id):
    return bool(update.effective_user) and update.effective_user.id == trainer_id


def register_booking_handlers(application: Application, trainer_tg_id: int):
    async def add_slot(update, context):
        if not _trainer(update, trainer_tg_id):
            return
        try:
            date_s, time_s = context.args[0], context.args[1]
            duration, capacity = int(context.args[2]), int(context.args[3])
        except (IndexError, ValueError):
            await update.message.reply_text(
                "/add_slot 2026-09-20 18:00 60 4"
            )
            return
        if duration < 15 or duration > 240 or capacity < 1 or capacity > 50:
            await update.message.reply_text("Длительность 15–240 минут, мест 1–50.")
            return
        conn = get_conn()
        try:
            slot_id = f"{date_s}_{time_s}"
            conn.execute(
                """INSERT INTO slots(id,date,time,duration,capacity)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET
                   duration=EXCLUDED.duration,capacity=EXCLUDED.capacity""",
                (slot_id, date_s, time_s, duration, capacity),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text(f"Слот сохранён: {date_s} {time_s}, мест: {capacity}")

    async def roster(update, context):
        if not _trainer(update, trainer_tg_id):
            return
        if not context.args:
            await update.message.reply_text("/roster 2026-09-20")
            return
        d = context.args[0]
        conn = get_conn()
        try:
            slots = conn.execute(
                "SELECT * FROM slots WHERE date=%s ORDER BY time", (d,)
            ).fetchall()
            lines = []
            for s in slots:
                names = conn.execute(
                    """SELECT client_name FROM bookings
                       WHERE slot_id=%s AND status='active'
                       ORDER BY created_at""",
                    (s["id"],),
                ).fetchall()
                names_s = ", ".join(x["client_name"] for x in names) or "пусто"
                lines.append(f"{s['time']} ({len(names)}/{s['capacity']}): {names_s}")
        finally:
            conn.close()
        await update.message.reply_text(
            f"📋 {d}\n" + ("\n".join(lines) if lines else "Слотов нет.")
        )

    async def clients(update, context):
        if not _trainer(update, trainer_tg_id):
            return
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT telegram_id,first_name,username
                   FROM known_users ORDER BY first_seen DESC"""
            ).fetchall()
        finally:
            conn.close()
        await update.message.reply_text(
            "Клиенты:\n" + (
                "\n".join(
                    f"{r['telegram_id']} — {r['first_name'] or ''}"
                    + (f" (@{r['username']})" if r["username"] else "")
                    for r in rows
                ) if rows else "Пока клиентов нет."
            )
        )

    async def set_program(update, context):
        if not _trainer(update, trainer_tg_id) or len(context.args) < 2:
            await update.message.reply_text(
                "/set_program <telegram_id> <текст программы>"
            )
            return
        try:
            client_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("telegram_id должен быть числом.")
            return
        text = " ".join(context.args[1:]).strip()
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO client_profiles(telegram_id,program_text,updated_at)
                   VALUES(%s,%s,%s)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   program_text=EXCLUDED.program_text,
                   updated_at=EXCLUDED.updated_at""",
                (client_id, text, datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("Программа сохранена.")

    async def set_kbju(update, context):
        if not _trainer(update, trainer_tg_id):
            return
        try:
            client_id, cal, p, f, c = map(int, context.args[:5])
        except (ValueError, IndexError):
            await update.message.reply_text(
                "/set_kbju <id> <ккал> <белки> <жиры> <углеводы>"
            )
            return
        if not (500 <= cal <= 10000 and 0 <= p <= 1000 and 0 <= f <= 500 and 0 <= c <= 1500):
            await update.message.reply_text("Проверь диапазоны КБЖУ.")
            return
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO client_profiles
                   (telegram_id,calories,protein,fat,carbs,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   calories=EXCLUDED.calories,protein=EXCLUDED.protein,
                   fat=EXCLUDED.fat,carbs=EXCLUDED.carbs,
                   updated_at=EXCLUDED.updated_at""",
                (client_id, cal, p, f, c, datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("КБЖУ сохранены.")

    application.add_handler(CommandHandler("add_slot", add_slot))
    application.add_handler(CommandHandler("roster", roster))
    application.add_handler(CommandHandler("clients", clients))
    application.add_handler(CommandHandler("set_program", set_program))
    application.add_handler(CommandHandler("set_kbju", set_kbju))


async def set_booking_menu_button(application: Application, miniapp_url: str):
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Запись",
            web_app=WebAppInfo(url=miniapp_url),
        )
    )
