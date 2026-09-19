from datetime import date, timedelta, datetime

from telegram.ext import CommandHandler, ContextTypes

from booking_api import get_conn

HORIZON_DAYS = 21
DAY_MAP = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}


def generate_upcoming_slots():
    conn = get_conn()
    try:
        template = conn.execute(
            "SELECT day_of_week,time,duration,capacity FROM weekly_template"
        ).fetchall()
        today = date.today()
        for i in range(HORIZON_DAYS):
            d = today + timedelta(days=i)
            for row in template:
                if d.weekday() != row["day_of_week"]:
                    continue
                slot_id = f"{d.isoformat()}_{row['time']}"
                conn.execute(
                    """INSERT INTO slots(id,date,time,duration,capacity)
                       VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (
                        slot_id, d.isoformat(), row["time"],
                        row["duration"], row["capacity"],
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def register_schedule_handlers(application, trainer_tg_id):
    async def set_weekly(update, context):
        if update.effective_user.id != trainer_tg_id:
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "/set_weekly пн,вт,ср 07:00,08:00,19:00 [мин] [мест]"
            )
            return
        days = [x.strip().lower() for x in context.args[0].split(",")]
        times = [x.strip() for x in context.args[1].split(",")]
        duration = int(context.args[2]) if len(context.args) > 2 and context.args[2].isdigit() else 60
        capacity = int(context.args[3]) if len(context.args) > 3 and context.args[3].isdigit() else 1
        if any(d not in DAY_MAP for d in days):
            await update.message.reply_text("Неизвестный день недели.")
            return
        try:
            for t in times:
                datetime.strptime(t, "%H:%M")
        except ValueError:
            await update.message.reply_text("Время должно быть ЧЧ:ММ.")
            return
        if not 15 <= duration <= 240 or not 1 <= capacity <= 50:
            await update.message.reply_text("Длительность 15–240, мест 1–50.")
            return

        conn = get_conn()
        try:
            for d in days:
                for t in times:
                    dow = DAY_MAP[d]
                    conn.execute(
                        """INSERT INTO weekly_template(id,day_of_week,time,duration,capacity)
                           VALUES(%s,%s,%s,%s,%s)
                           ON CONFLICT(id) DO UPDATE SET
                           duration=EXCLUDED.duration,capacity=EXCLUDED.capacity""",
                        (f"{dow}_{t}", dow, t, duration, capacity),
                    )
            conn.commit()
        finally:
            conn.close()
        generate_upcoming_slots()
        await update.message.reply_text("Постоянное расписание сохранено и слоты созданы.")

    async def show_weekly(update, context):
        if update.effective_user.id != trainer_tg_id:
            return
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT day_of_week,time FROM weekly_template ORDER BY day_of_week,time"
            ).fetchall()
        finally:
            conn.close()
        names = {v:k for k,v in DAY_MAP.items()}
        await update.message.reply_text(
            "\n".join(f"{names[r['day_of_week']]} {r['time']}" for r in rows)
            if rows else "Расписание не задано."
        )

    async def clear_weekly(update, context):
        if update.effective_user.id != trainer_tg_id or not context.args:
            await update.message.reply_text("/clear_weekly пн,вт")
            return
        days = [x.strip().lower() for x in context.args[0].split(",")]
        conn = get_conn()
        try:
            for d in days:
                if d in DAY_MAP:
                    conn.execute("DELETE FROM weekly_template WHERE day_of_week=%s", (DAY_MAP[d],))
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("Шаблон расписания обновлён.")

    application.add_handler(CommandHandler("set_weekly", set_weekly))
    application.add_handler(CommandHandler("weekly_schedule", show_weekly))
    application.add_handler(CommandHandler("clear_weekly", clear_weekly))

    async def daily_job(context: ContextTypes.DEFAULT_TYPE):
        try:
            generate_upcoming_slots()
        except Exception:
            context.application.logger.exception("slot generation failed")

    application.job_queue.run_repeating(daily_job, interval=86400, first=20)
