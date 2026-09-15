"""
Technique check: client sends a video of an exercise, it's forwarded to
the trainer with context, the trainer replies to that video with feedback
(optionally starting with a 1-10 score), and the reply is relayed back
to the client automatically.

NOT automated AI video analysis on purpose — see the chat reply for why.
The bot only handles routing; the judgment stays with the trainer.

Register with:
    from technique_handlers import register_technique_handlers
    register_technique_handlers(application, trainer_tg_id=TRAINER_TG_ID)
"""

import re
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from booking_api import get_conn


def register_technique_handlers(application: Application, trainer_tg_id: int):
    async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        video = update.message.video
        exercise = (update.message.caption or "не указано").strip()

        sent = await context.bot.send_video(
            trainer_tg_id, video.file_id,
            caption=(
                f"Техника: {exercise}\n"
                f"От: {user.first_name or ''} (id {user.id})\n\n"
                f"Ответь на это видео текстом — это и будет разбор. Можно начать с числа "
                f"1-10, оно уйдёт как оценка, остальное как комментарий."
            ),
        )

        conn = get_conn()
        review_id = f"tech_{user.id}_{int(datetime.utcnow().timestamp())}"
        conn.execute(
            "INSERT INTO technique_reviews "
            "(id, telegram_id, exercise, video_file_id, trainer_message_id, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
            (review_id, user.id, exercise, video.file_id, sent.message_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text("Видео отправлено на разбор. Пришлю ответ, как только тренер оценит.")

    async def handle_trainer_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
        replied_id = update.message.reply_to_message.message_id
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM technique_reviews WHERE trainer_message_id = %s AND status = 'pending'",
            (replied_id,),
        ).fetchone()
        if not row:
            conn.close()
            return  # trainer replying to something else — not ours to handle

        text = update.message.text.strip()
        score, feedback = None, text
        m = re.match(r"^(\d{1,2})\b\s*(.*)", text, re.DOTALL)
        if m and 1 <= int(m.group(1)) <= 10:
            score = int(m.group(1))
            feedback = m.group(2).strip() or "(без комментария)"

        conn.execute(
            "UPDATE technique_reviews SET score = %s, feedback = %s, status = 'reviewed', reviewed_at = %s WHERE id = %s",
            (score, feedback, datetime.utcnow().isoformat(), row["id"]),
        )
        conn.commit()
        conn.close()

        score_line = f"Оценка: {score}/10\n" if score else ""
        await context.bot.send_message(
            row["telegram_id"],
            f"Разбор техники ({row['exercise']}):\n{score_line}{feedback}",
        )
        await update.message.reply_text("Отправлено клиенту.")

    async def pending_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != trainer_tg_id:
            return
        conn = get_conn()
        rows = conn.execute(
            "SELECT exercise, telegram_id, created_at FROM technique_reviews "
            "WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Нет видео на разборе.")
            return
        lines = [f"{r['exercise']} — id {r['telegram_id']} ({r['created_at'][:16]})" for r in rows]
        await update.message.reply_text("На разборе:\n" + "\n".join(lines))

    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.REPLY & ~filters.COMMAND & filters.User(trainer_tg_id),
            handle_trainer_reply,
        )
    )
    application.add_handler(CommandHandler("pending_reviews", pending_reviews))
