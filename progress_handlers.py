"""
Client logs working weights per exercise; trainer pulls a progress chart
per client/exercise instead of relying on memory or attendance alone.

Register with:
    from progress_handlers import register_progress_handlers
    register_progress_handlers(application, trainer_tg_id=TRAINER_TG_ID)
"""

import io
from datetime import datetime, date

import matplotlib
matplotlib.use("Agg")  # no display available on a server — render to buffer only
import matplotlib.pyplot as plt

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from booking_api import get_conn


def register_progress_handlers(application: Application, trainer_tg_id: int):
    async def log_lift(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            exercise = context.args[0].lower()
            weight = float(context.args[1])
            reps = int(context.args[2])
            sets = int(context.args[3])
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Формат: /log_lift упражнение вес повторения подходы\n"
                "Пример: /log_lift жим 80 8 4"
            )
            return
        conn = get_conn()
        log_id = f"lift_{update.effective_user.id}_{int(datetime.utcnow().timestamp())}"
        conn.execute(
            "INSERT INTO progress_logs (id, telegram_id, log_date, exercise, weight_kg, reps, sets, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (log_id, update.effective_user.id, date.today().isoformat(), exercise, weight, reps, sets,
             datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Записано: {exercise} {weight} кг × {reps} × {sets}")

    async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        if len(context.args) < 2:
            await update.message.reply_text("Формат: /progress <id клиента> <упражнение>")
            return
        client_id = int(context.args[0])
        exercise = " ".join(context.args[1:]).lower()

        conn = get_conn()
        rows = conn.execute(
            "SELECT log_date, weight_kg, reps, sets FROM progress_logs "
            "WHERE telegram_id = %s AND exercise = %s ORDER BY log_date",
            (client_id, exercise),
        ).fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("Записей по этому упражнению нет.")
            return

        dates = [r["log_date"] for r in rows]
        weights = [r["weight_kg"] for r in rows]

        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(dates, weights, marker="o")
        ax.set_title(f"{exercise} — рабочий вес, кг")
        ax.tick_params(axis="x", rotation=45)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)

        last = rows[-1]
        await update.message.reply_photo(
            buf, caption=f"Последняя запись: {last['weight_kg']} кг × {last['reps']} × {last['sets']}"
        )

    application.add_handler(CommandHandler("log_lift", log_lift))
    application.add_handler(CommandHandler("progress", progress))
