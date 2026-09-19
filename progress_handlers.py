import io
import time
from datetime import date, datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telegram.ext import CommandHandler

from booking_api import get_conn


def register_progress_handlers(application, trainer_tg_id):
    async def log_lift(update, context):
        if len(context.args) != 4:
            await update.message.reply_text("/log_lift упражнение вес повторения подходы")
            return
        try:
            exercise = context.args[0].lower()
            weight = float(context.args[1])
            reps = int(context.args[2])
            sets = int(context.args[3])
            if not 0 < weight <= 1000 or not 1 <= reps <= 100 or not 1 <= sets <= 50:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Проверь вес, повторения и подходы.")
            return
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO progress_logs
                   (id,telegram_id,log_date,exercise,weight_kg,reps,sets,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    f"lift_{update.effective_user.id}_{time.time_ns()}",
                    update.effective_user.id, date.today().isoformat(),
                    exercise, weight, reps, sets, datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text(f"Записано: {exercise} {weight:g} × {reps} × {sets}")

    async def trainer_progress(update, context):
        if update.effective_user.id != trainer_tg_id or len(context.args) < 2:
            await update.message.reply_text("/progress <id> <упражнение>")
            return
        try:
            client_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID должен быть числом.")
            return
        exercise = " ".join(context.args[1:]).lower()
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT log_date,weight_kg,reps,sets FROM progress_logs
                   WHERE telegram_id=%s AND exercise=%s ORDER BY created_at""",
                (client_id, exercise),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            await update.message.reply_text("Записей нет.")
            return
        fig, ax = plt.subplots(figsize=(7, 3.5))
        ax.plot([r["log_date"] for r in rows], [r["weight_kg"] for r in rows], marker="o")
        ax.set_title(f"{exercise} — рабочий вес")
        ax.set_ylabel("кг")
        ax.tick_params(axis="x", rotation=45)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140)
        plt.close(fig)
        buf.seek(0)
        last = rows[-1]
        await update.message.reply_photo(
            buf,
            caption=f"Последняя запись: {last['weight_kg']} кг × {last['reps']} × {last['sets']}",
        )

    application.add_handler(CommandHandler("log_lift", log_lift))
    application.add_handler(CommandHandler("progress", trainer_progress))
