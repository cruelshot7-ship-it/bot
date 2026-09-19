import os
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo

from booking_api import get_conn

TZ = ZoneInfo(os.getenv("TRAINER_TZ", "Europe/Minsk"))


def register_jobs(application, trainer_tg_id):
    async def reminders(context):
        now = datetime.now(timezone.utc)
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT b.id,b.client_tg_id,s.date,s.time,b.reminded_24h,b.reminded_2h
                   FROM bookings b JOIN slots s ON s.id=b.slot_id
                   WHERE b.status='active' AND (b.reminded_24h=0 OR b.reminded_2h=0)"""
            ).fetchall()
            for r in rows:
                local = datetime.strptime(
                    f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=TZ)
                hours = (local.astimezone(timezone.utc) - now).total_seconds() / 3600
                if not r["reminded_24h"] and 23.5 <= hours <= 24.5:
                    try:
                        await context.bot.send_message(
                            r["client_tg_id"],
                            f"Напоминание: завтра в {r['time']} у тебя тренировка.",
                        )
                        conn.execute(
                            "UPDATE bookings SET reminded_24h=1 WHERE id=%s", (r["id"],)
                        )
                    except Exception:
                        context.application.logger.exception("24h reminder failed")
                if not r["reminded_2h"] and 1.5 <= hours <= 2.5:
                    try:
                        await context.bot.send_message(
                            r["client_tg_id"],
                            f"Напоминание: через 2 часа ({r['time']}) у тебя тренировка.",
                        )
                        conn.execute(
                            "UPDATE bookings SET reminded_2h=1 WHERE id=%s", (r["id"],)
                        )
                    except Exception:
                        context.application.logger.exception("2h reminder failed")
            conn.commit()
        finally:
            conn.close()

    async def weekly_summary(context):
        since = (datetime.now(TZ).date() - timedelta(days=6)).isoformat()
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT f.telegram_id,u.first_name,
                          COALESCE(SUM(f.calories),0) cal,COUNT(*) entries
                   FROM food_logs f LEFT JOIN known_users u
                   ON u.telegram_id=f.telegram_id
                   WHERE f.log_date >= %s
                   GROUP BY f.telegram_id,u.first_name
                   ORDER BY cal DESC""",
                (since,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return
        lines = ["📊 КБЖУ за последние 7 дней"]
        for r in rows:
            lines.append(
                f"{r['first_name'] or r['telegram_id']}: "
                f"{r['entries']} записей, ~{round(r['cal']/7)} ккал/день"
            )
        await context.bot.send_message(trainer_tg_id, "\n".join(lines))

    application.job_queue.run_repeating(reminders, interval=600, first=15)
    application.job_queue.run_daily(
        weekly_summary,
        time=dt_time(20, 0, tzinfo=TZ),
        days=(6,),
    )
