import re
import time
from datetime import datetime

from telegram.ext import CommandHandler, MessageHandler, filters

from booking_api import get_conn


def register_technique_handlers(application, trainer_tg_id):
    async def video_handler(update, context):
        user = update.effective_user
        video = update.message.video
        exercise = (update.message.caption or "не указано").strip()[:200]
        sent = await context.bot.send_video(
            trainer_tg_id,
            video.file_id,
            caption=(
                f"Техника: {exercise}\n"
                f"Клиент: {user.first_name or ''} (id {user.id})\n\n"
                "Ответь на это видео текстом. Можно начать с оценки 1-10."
            ),
        )
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO technique_reviews
                   (id,telegram_id,exercise,video_file_id,trainer_message_id,status,created_at)
                   VALUES(%s,%s,%s,%s,%s,'pending',%s)""",
                (
                    f"tech_{user.id}_{time.time_ns()}",
                    user.id, exercise, video.file_id, sent.message_id,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("Видео отправлено тренеру.")

    async def trainer_reply(update, context):
        if update.effective_user.id != trainer_tg_id:
            return
        replied = update.message.reply_to_message
        if not replied:
            return
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM technique_reviews
                   WHERE trainer_message_id=%s AND status='pending'""",
                (replied.message_id,),
            ).fetchone()
            if not row:
                return
            text = update.message.text.strip()
            score = None
            feedback = text
            m = re.match(r"^(\d{1,2})\b\s*(.*)", text, re.S)
            if m and 1 <= int(m.group(1)) <= 10:
                score = int(m.group(1))
                feedback = m.group(2).strip() or "Без комментария"
            conn.execute(
                """UPDATE technique_reviews SET score=%s,feedback=%s,status='reviewed',reviewed_at=%s
                   WHERE id=%s""",
                (score, feedback, datetime.utcnow().isoformat(), row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        await context.bot.send_message(
            row["telegram_id"],
            f"Разбор техники ({row['exercise']}):\n"
            + (f"Оценка: {score}/10\n" if score else "")
            + feedback,
        )
        await update.message.reply_text("Разбор отправлен клиенту.")

    async def pending(update, context):
        if update.effective_user.id != trainer_tg_id:
            return
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT exercise,telegram_id,created_at FROM technique_reviews
                   WHERE status='pending' ORDER BY created_at"""
            ).fetchall()
        finally:
            conn.close()
        await update.message.reply_text(
            "На разборе:\n" + (
                "\n".join(f"{r['exercise']} — {r['telegram_id']}" for r in rows)
                if rows else "нет видео"
            )
        )

    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.REPLY & ~filters.COMMAND & filters.User(trainer_tg_id),
            trainer_reply,
        )
    )
    application.add_handler(CommandHandler("pending_reviews", pending))
