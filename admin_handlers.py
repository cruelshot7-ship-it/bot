from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from booking_api import get_conn
from schedule_handlers import generate_upcoming_slots

MAIN, CLIENTS, PROGRAM, KBJU = range(4)


def register_admin_handlers(application, trainer_tg_id):
    def is_trainer(update):
        return update.effective_user and update.effective_user.id == trainer_tg_id

    menu = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Клиенты", callback_data="clients")],
        [InlineKeyboardButton("📅 Обновить слоты", callback_data="slots")],
    ])

    async def admin(update, context):
        if not is_trainer(update):
            return ConversationHandler.END
        await update.message.reply_text("Панель тренера:", reply_markup=menu)
        return MAIN

    async def main_click(update, context):
        q = update.callback_query
        await q.answer()
        if not is_trainer(update):
            return ConversationHandler.END
        if q.data == "slots":
            generate_upcoming_slots()
            await q.edit_message_text("Слоты на ближайшие 21 день обновлены.", reply_markup=menu)
            return MAIN
        if q.data == "clients":
            conn = get_conn()
            try:
                rows = conn.execute(
                    """SELECT telegram_id,first_name,username FROM known_users
                       WHERE telegram_id <> %s ORDER BY first_seen DESC LIMIT 50""",
                    (trainer_tg_id,),
                ).fetchall()
            finally:
                conn.close()
            buttons = [
                [InlineKeyboardButton(
                    f"{r['first_name'] or r['telegram_id']}"
                    + (f" @{r['username']}" if r["username"] else ""),
                    callback_data=f"client:{r['telegram_id']}"
                )]
                for r in rows
            ]
            buttons.append([InlineKeyboardButton("‹ Назад", callback_data="back")])
            await q.edit_message_text(
                "Выбери клиента:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return CLIENTS
        if q.data == "back":
            await q.edit_message_text("Панель тренера:", reply_markup=menu)
        return MAIN

    async def client_click(update, context):
        q = update.callback_query
        await q.answer()
        if not is_trainer(update):
            return ConversationHandler.END
        if q.data == "back":
            await q.edit_message_text("Панель тренера:", reply_markup=menu)
            return MAIN
        client_id = int(q.data.split(":")[1])
        context.user_data["admin_client_id"] = client_id
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Программа", callback_data="program")],
            [InlineKeyboardButton("КБЖУ", callback_data="kbju")],
            [InlineKeyboardButton("‹ Назад", callback_data="clients")],
        ])
        await q.edit_message_text(f"Клиент {client_id}", reply_markup=kb)
        return CLIENTS

    async def client_action(update, context):
        q = update.callback_query
        await q.answer()
        if q.data == "program":
            await q.edit_message_text("Пришли текст программы одним сообщением.")
            return PROGRAM
        if q.data == "kbju":
            await q.edit_message_text("Пришли: калории белки жиры углеводы")
            return KBJU
        if q.data == "clients":
            return await main_click(update, context)
        return CLIENTS

    async def program_text(update, context):
        if not is_trainer(update):
            return ConversationHandler.END
        cid = context.user_data.get("admin_client_id")
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO client_profiles(telegram_id,program_text,updated_at)
                   VALUES(%s,%s,NOW()::text)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   program_text=EXCLUDED.program_text,updated_at=EXCLUDED.updated_at""",
                (cid, update.message.text.strip()),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("Программа сохранена.", reply_markup=menu)
        return MAIN

    async def kbju_text(update, context):
        if not is_trainer(update):
            return ConversationHandler.END
        parts = update.message.text.split()
        if len(parts) != 4:
            await update.message.reply_text("Нужно 4 числа.")
            return KBJU
        try:
            cal, p, f, c = map(int, parts)
        except ValueError:
            await update.message.reply_text("Только числа.")
            return KBJU
        cid = context.user_data.get("admin_client_id")
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO client_profiles
                   (telegram_id,calories,protein,fat,carbs,updated_at)
                   VALUES(%s,%s,%s,%s,%s,NOW()::text)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   calories=EXCLUDED.calories,protein=EXCLUDED.protein,
                   fat=EXCLUDED.fat,carbs=EXCLUDED.carbs,
                   updated_at=EXCLUDED.updated_at""",
                (cid, cal, p, f, c),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text("КБЖУ сохранены.", reply_markup=menu)
        return MAIN

    conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin)],
        states={
            MAIN: [CallbackQueryHandler(main_click, pattern="^(clients|slots|back)$")],
            CLIENTS: [
                CallbackQueryHandler(client_click, pattern=r"^client:\d+$"),
                CallbackQueryHandler(client_action, pattern=r"^(program|kbju|clients)$"),
                CallbackQueryHandler(main_click, pattern="^back$"),
            ],
            PROGRAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, program_text)],
            KBJU: [MessageHandler(filters.TEXT & ~filters.COMMAND, kbju_text)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
    )
    application.add_handler(conv)
