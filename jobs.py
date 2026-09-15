"""
Two scheduled jobs:
  1. Reminder check, every 10 min — messages clients ~24h and ~2h before
     their booked session. State lives in the DB (reminded_24h/reminded_2h
     columns), not in memory, so it survives restarts/redeploys cleanly.
  2. Weekly КБЖУ summary — every Sunday, sends the trainer a per-client
     recap of the week's logged nutrition.

REQUIRES the job-queue extra:
    pip install "python-telegram-bot[job-queue]"
(already in requirements.txt) — without it, application.job_queue is None
and these silently never run.

ENV VAR (optional):
    TRAINER_TZ - IANA timezone for interpreting slot times and scheduling
    the Sunday summary, default "Europe/Minsk". Slot dates/times are
    assumed to already be in this timezone — if you enter slots in a
    different zone, reminders will fire at the wrong local time.

Register with:
    from jobs import register_jobs
    register_jobs(application, trainer_tg_id=TRAINER_TG_ID)
"""

import os
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from booking_api import get_conn

TRAINER_TZ = ZoneInfo(os.environ.get("TRAINER_TZ", "Europe/Minsk"))


def _slot_datetime_utc(date_str: str, time_str: str) -> datetime:
    local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=TRAINER_TZ)
    return local.astimezone(timezone.utc)


def register_jobs(application: Application, trainer_tg_id: int):
    async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
        now = datetime.now(timezone.utc)
        conn = get_conn()
        rows = conn.execute("""
            SELECT b.id, b.client_tg_id, s.date, s.time, b.reminded_24h, b.reminded_2h
            FROM bookings b JOIN slots s ON b.slot_id = s.id
            WHERE b.status = 'active' AND (b.reminded_24h = 0 OR b.reminded_2h = 0)
        """).fetchall()

        for r in rows:
            slot_dt = _slot_datetime_utc(r["date"], r["time"])
            hours_until = (slot_dt - now).total_seconds() / 3600

            if not r["reminded_24h"] and 23.75 <= hours_until <= 24.25:
                await context.bot.send_message(
                    r["client_tg_id"], f"Напоминание: завтра в {r['time']} у тебя тренировка."
                )
                conn.execute("UPDATE bookings SET reminded_24h = 1 WHERE id = %s", (r["id"],))

            if not r["reminded_2h"] and 1.75 <= hours_until <= 2.25:
                await context.bot.send_message(
                    r["client_tg_id"], f"Напоминание: через 2 часа ({r['time']}) у тебя тренировка."
                )
                conn.execute("UPDATE bookings SET reminded_2h = 1 WHERE id = %s", (r["id"],))

        conn.commit()
        conn.close()

    async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
        week_ago = (datetime.now(TRAINER_TZ).date() - timedelta(days=7)).isoformat()
        conn = get_conn()
        rows = conn.execute("""
            SELECT f.telegram_id, u.first_name,
                   COALESCE(SUM(f.calories), 0) AS cal,
                   COUNT(*) AS entries
            FROM food_logs f
            LEFT JOIN known_users u ON u.telegram_id = f.telegram_id
            WHERE f.log_date >= %s
            GROUP BY f.telegram_id
            ORDER BY cal DESC
        """, (week_ago,)).fetchall()
        conn.close()

        if not rows:
            return
        lines = ["КБЖУ за неделю:"]
        for r in rows:
            name = r["first_name"] or str(r["telegram_id"])
            avg_cal = round(r["cal"] / 7)
            lines.append(f"{name}: {r['entries']} записей, в среднем {avg_cal} ккал/день")
        await context.bot.send_message(trainer_tg_id, "\n".join(lines))

    application.job_queue.run_repeating(check_reminders, interval=600, first=10)
    application.job_queue.run_daily(
        weekly_summary,
        time=dt_time(hour=20, minute=0, tzinfo=TRAINER_TZ),
        days=(6,),  # Sunday
    )
