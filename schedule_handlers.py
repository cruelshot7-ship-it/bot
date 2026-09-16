"""
Set your recurring weekly availability once — slots for the next 21 days
get generated automatically from it every day, so you never retype the
same week by hand again.

Existing single slots from /add_slot aren't touched or overwritten by
this — the generator only fills in day/time combos that don't already
have a slot, so any one-off manual change stays put.

Register with:
    from schedule_handlers import register_schedule_handlers
    register_schedule_handlers(application, trainer_tg_id=TRAINER_TG_ID)
"""

from datetime import date, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from booking_api import get_conn

HORIZON_DAYS = 21  # how far ahead slots get auto-generated

DAY_MAP = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_NAMES = {v: k for k, v in DAY_MAP.items()}


async def generate_upcoming_slots():
    conn = get_conn()
    template = conn.execute(
        "SELECT day_of_week, time, duration, capacity FROM weekly_template"
    ).fetchall()
    if not template:
        conn.close()
        return

    by_dow = {}
    for r in template:
        by_dow.setdefault(r["day_of_week"], []).append(r)

    today = date.today()
    for i in range(HORIZON_DAYS):
        d = today + timedelta(days=i)
        for e in by_dow.get(d.weekday(), []):
            slot_id = f"{d.isoformat()}_{e['time']}"
            conn.execute(
                "INSERT INTO slots (id, date, time, duration, capacity) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (slot_id, d.isoformat(), e["time"], e["duration"], e["capacity"]),
            )
    conn.commit()
    conn.close()


def register_schedule_handlers(application: Application, trainer_tg_id: int):
    async def set_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        try:
            days_raw, times_raw = context.args[0], context.args[1]
            duration = int(context.args[2]) if len(context.args) > 2 else 60
            capacity = int(context.args[3]) if len(context.args) > 3 else 1
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Формат: /set_weekly дни времена [минуты] [мест]\n"
                "Дни через запятую, без пробелов: пн,вт,ср,чт,пт,сб,вс\n"
                "Пример: /set_weekly пн,вт,ср,чт,пт 07:00,08:00,08:30,09:00,10:00,19:00"
            )
            return

        days = [d.strip().lower() for d in days_raw.split(",")]
        times = [t.strip() for t in times_raw.split(",")]
        unknown = [d for d in days if d not in DAY_MAP]
        if unknown:
            await update.message.reply_text(
                f"Не понял дни: {', '.join(unknown)}. Используй пн,вт,ср,чт,пт,сб,вс"
            )
            return

        conn = get_conn()
        for d in days:
            dow = DAY_MAP[d]
            for t in times:
                entry_id = f"{dow}_{t}"
                conn.execute(
                    "INSERT INTO weekly_template (id, day_of_week, time, duration, capacity) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET duration = EXCLUDED.duration, "
                    "capacity = EXCLUDED.capacity",
                    (entry_id, dow, t, duration, capacity),
                )
        conn.commit()
        conn.close()

        await generate_upcoming_slots()  # apply now, don't wait for the nightly run
        await update.message.reply_text(
            f"Сохранено: {', '.join(days)} — {', '.join(times)}\n"
            f"Слоты на {HORIZON_DAYS} дней вперёд уже расставлены."
        )

    async def show_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        conn = get_conn()
        rows = conn.execute(
            "SELECT day_of_week, time FROM weekly_template ORDER BY day_of_week, time"
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Постоянное расписание ещё не задано.")
            return
        by_day = {}
        for r in rows:
            by_day.setdefault(r["day_of_week"], []).append(r["time"])
        lines = [f"{DAY_NAMES[d]}: {', '.join(times)}" for d, times in sorted(by_day.items())]
        await update.message.reply_text("Постоянное расписание:\n" + "\n".join(lines))

    async def clear_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        if not context.args:
            await update.message.reply_text("Формат: /clear_weekly пн,вт")
            return
        days = [d.strip().lower() for d in context.args[0].split(",")]
        unknown = [d for d in days if d not in DAY_MAP]
        if unknown:
            await update.message.reply_text(f"Не понял дни: {', '.join(unknown)}")
            return
        conn = get_conn()
        for d in days:
            conn.execute("DELETE FROM weekly_template WHERE day_of_week = %s", (DAY_MAP[d],))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Убрано из постоянного расписания: {', '.join(days)}")

    application.add_handler(CommandHandler("set_weekly", set_weekly))
    application.add_handler(CommandHandler("weekly_schedule", show_weekly))
    application.add_handler(CommandHandler("clear_weekly", clear_weekly))

    async def _daily_generate(context: ContextTypes.DEFAULT_TYPE):
        await generate_upcoming_slots()

    application.job_queue.run_repeating(_daily_generate, interval=86400, first=15)
