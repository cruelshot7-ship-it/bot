"""
Trainer control panel — button-driven, no command syntax to remember.

Wraps the exact same underlying actions as /set_weekly, /set_program,
/set_kbju, /clients, /roster — those text commands still work exactly
as before, this just adds a menu on top so you don't have to type them.

Entry point: /admin (trainer only — everyone else gets nothing).

Register with:
    from admin_handlers import register_admin_handlers
    register_admin_handlers(application, trainer_tg_id=TRAINER_TG_ID)
"""
from datetime import date, datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

from booking_api import get_conn
from schedule_handlers import generate_upcoming_slots, DAY_MAP, HORIZON_DAYS

MAIN, CHOOSE_CLIENT, CLIENT_MENU, AWAIT_PROGRAM, AWAIT_KBJU, AWAIT_CUSTOM = range(6)

# Твоё стандартное расписание. Поменяется — поменяй здесь, это единственное
# место, где оно захардкожено.
WEEKDAY_PRESET = {
    "days": ["пн", "вт", "ср", "чт", "пт"],
    "times": ["07:00", "08:00", "08:30", "09:00", "10:00", "16:30", "19:00"],
}
SATURDAY_PRESET = {
    "days": ["сб"],
    "times": ["10:00", "11:00"],
}


def _main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание", callback_data="adm:schedule")],
        [InlineKeyboardButton("👥 Клиенты", callback_data="adm:clients")],
        [InlineKeyboardButton("📋 Кто записан сегодня", callback_data="adm:roster:today")],
        [InlineKeyboardButton("📋 Кто записан завтра", callback_data="adm:roster:tomorrow")],
    ])


def register_admin_handlers(application: Application, trainer_tg_id: int):

    def _is_trainer(update: Update) -> bool:
        return bool(update.effective_user) and update.effective_user.id == trainer_tg_id

    async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_trainer(update):
            return ConversationHandler.END
        await update.message.reply_text("Панель тренера:", reply_markup=_main_menu_markup())
        return MAIN

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("admin_client_id", None)
        if update.message:
            await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    async def back_to_main(query):
        await query.edit_message_text("Панель тренера:", reply_markup=_main_menu_markup())
        return MAIN

    # ---------- Главное меню ----------

    async def show_clients(query):
        conn = get_conn()
        rows = conn.execute(
            "SELECT telegram_id, first_name, username FROM known_users ORDER BY first_seen DESC LIMIT 40"
        ).fetchall()
        conn.close()
        if not rows:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data="adm:back")]])
            await query.edit_message_text("Пока никто не открывал мини-эп.", reply_markup=markup)
            return MAIN
        buttons = [
            [InlineKeyboardButton(
                (r["first_name"] or str(r["telegram_id"])) + (f" (@{r['username']})" if r["username"] else ""),
                callback_data=f"adm:client:{r['telegram_id']}",
            )]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("‹ Назад", callback_data="adm:back")])
        await query.edit_message_text("Выбери клиента:", reply_markup=InlineKeyboardMarkup(buttons))
        return CHOOSE_CLIENT

    async def show_roster(query, target_date):
        conn = get_conn()
        slots = conn.execute(
            "SELECT * FROM slots WHERE date = %s ORDER BY time", (target_date.isoformat(),)
        ).fetchall()
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("‹ Назад", callback_data="adm:back")]])
        if not slots:
            conn.close()
            await query.edit_message_text(f"На {target_date.isoformat()} слотов нет.", reply_markup=markup)
            return
        lines = []
        for s in slots:
            names = conn.execute(
                "SELECT client_name FROM bookings WHERE slot_id = %s AND status = 'active'",
                (s["id"],),
            ).fetchall()
            names_str = ", ".join(n["client_name"] for n in names) if names else "пусто"
            lines.append(f"{s['time']} ({len(names)}/{s['capacity']}): {names_str}")
        conn.close()
        await query.edit_message_text(
            f"Запись на {target_date.isoformat()}:\n" + "\n".join(lines), reply_markup=markup
        )

    async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not _is_trainer(update):
            return ConversationHandler.END
        action = query.data

        if action == "adm:schedule":
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"Будни {WEEKDAY_PRESET['times'][0]}–{WEEKDAY_PRESET['times'][-1]}",
                    callback_data="adm:schedule:weekday",
                )],
                [InlineKeyboardButton(
                    f"Суббота {', '.join(SATURDAY_PRESET['times'])}",
                    callback_data="adm:schedule:saturday",
                )],
                [InlineKeyboardButton("Своё расписание", callback_data="adm:schedule:custom")],
                [InlineKeyboardButton("‹ Назад", callback_data="adm:back")],
            ])
            await query.edit_message_text("Что поставить?", reply_markup=markup)
            return MAIN

        if action == "adm:clients":
            return await show_clients(query)

        if action in ("adm:roster:today", "adm:roster:tomorrow"):
            target = date.today() if action.endswith("today") else date.today() + timedelta(days=1)
            await show_roster(query, target)
            return MAIN

        if action == "adm:back":
            return await back_to_main(query)

        return MAIN

    # ---------- Расписание (пресеты и своё) ----------

    async def apply_weekly(days, times):
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
                    (entry_id, dow, t, 60, 1),
                )
        conn.commit()
        conn.close()
        await generate_upcoming_slots()

    async def on_schedule_preset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer("Ставлю...")
        if not _is_trainer(update):
            return ConversationHandler.END
        preset = WEEKDAY_PRESET if query.data == "adm:schedule:weekday" else SATURDAY_PRESET
        await apply_weekly(preset["days"], preset["times"])
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("‹ В меню", callback_data="adm:back")]])
        await query.edit_message_text(
            f"Готово: {', '.join(preset['days'])} — {', '.join(preset['times'])}\n"
            f"Слоты на {HORIZON_DAYS} дней вперёд расставлены.",
            reply_markup=markup,
        )
        return MAIN

    async def on_schedule_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not _is_trainer(update):
            return ConversationHandler.END
        await query.edit_message_text(
            "Пришли одним сообщением: дни;времена\n"
            "Дни через запятую без пробелов (пн,вт,ср,чт,пт,сб,вс).\n"
            "Пример: пн,вт,ср,чт,пт;07:00,08:00,19:00"
        )
        return AWAIT_CUSTOM

    async def on_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_trainer(update):
            return ConversationHandler.END
        text = update.message.text.strip()
        try:
            days_raw, times_raw = text.split(";")
        except ValueError:
            await update.message.reply_text("Формат: дни;времена — например пн,вт;07:00,08:00. Ещё раз:")
            return AWAIT_CUSTOM
        days = [d.strip().lower() for d in days_raw.split(",")]
        times = [t.strip() for t in times_raw.split(",")]
        unknown = [d for d in days if d not in DAY_MAP]
        if unknown:
            await update.message.reply_text(f"Не понял дни: {', '.join(unknown)}. Ещё раз:")
            return AWAIT_CUSTOM
        await apply_weekly(days, times)
        await update.message.reply_text(
            f"Готово: {', '.join(days)} — {', '.join(times)}",
            reply_markup=_main_menu_markup(),
        )
        return MAIN

    # ---------- Клиенты ----------

    async def on_clients_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not _is_trainer(update):
            return ConversationHandler.END
        return await show_clients(query)

    async def on_client_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not _is_trainer(update):
            return ConversationHandler.END
        client_id = int(query.data.split(":")[2])
        context.user_data["admin_client_id"] = client_id
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Назначить программу", callback_data="adm:client:program")],
            [InlineKeyboardButton("Указать КБЖУ", callback_data="adm:client:kbju")],
            [InlineKeyboardButton("Показать профиль", callback_data="adm:client:profile")],
            [InlineKeyboardButton("‹ К списку клиентов", callback_data="adm:clients")],
        ])
        await query.edit_message_text(f"Клиент {client_id}. Что сделать?", reply_markup=markup)
        return CLIENT_MENU

    async def on_client_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not _is_trainer(update):
            return ConversationHandler.END
        client_id = context.user_data.get("admin_client_id")
        if not client_id:
            return await back_to_main(query)

        if query.data == "adm:client:program":
            await query.edit_message_text(f"Пришли текст программы для {client_id} одним сообщением:")
            return AWAIT_PROGRAM

        if query.data == "adm:client:kbju":
            await query.edit_message_text(
                f"Пришли для {client_id} 4 числа через пробел: калории белки жиры углеводы\n"
                f"Пример: 2400 180 70 250"
            )
            return AWAIT_KBJU

        if query.data == "adm:client:profile":
            conn = get_conn()
            row = conn.execute(
                "SELECT * FROM client_profiles WHERE telegram_id = %s", (client_id,)
            ).fetchone()
            conn.close()
            if not row:
                text = f"У {client_id} пока ничего не назначено."
            else:
                text = (
                    f"Клиент {client_id}\n"
                    f"Программа: {row['program_text'] or '—'}\n"
                    f"КБЖУ: {row['calories'] or '—'} ккал, "
                    f"Б{row['protein'] or '—'}/Ж{row['fat'] or '—'}/У{row['carbs'] or '—'}"
                )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("‹ Назад", callback_data=f"adm:client:{client_id}")],
            ])
            await query.edit_message_text(text, reply_markup=markup)
            return CLIENT_MENU

        return CLIENT_MENU

    async def on_program_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_trainer(update):
            return ConversationHandler.END
        client_id = context.user_data.get("admin_client_id")
        text = update.message.text.strip()
        conn = get_conn()
        conn.execute(
            "INSERT INTO client_profiles (telegram_id, program_text, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET program_text = EXCLUDED.program_text, "
            "updated_at = EXCLUDED.updated_at",
            (client_id, text, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("Программа сохранена.", reply_markup=_main_menu_markup())
        return MAIN

    async def on_kbju_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_trainer(update):
            return ConversationHandler.END
        client_id = context.user_data.get("admin_client_id")
        parts = update.message.text.split()
        if len(parts) != 4:
            await update.message.reply_text("Нужно ровно 4 числа через пробел. Ещё раз:")
            return AWAIT_KBJU
        try:
            calories, protein, fat, carbs = (int(p) for p in parts)
        except ValueError:
            await update.message.reply_text("Это должны быть целые числа. Ещё раз:")
            return AWAIT_KBJU
        conn = get_conn()
        conn.execute(
            "INSERT INTO client_profiles (telegram_id, calories, protein, fat, carbs, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET calories = EXCLUDED.calories, "
            "protein = EXCLUDED.protein, fat = EXCLUDED.fat, carbs = EXCLUDED.carbs, "
            "updated_at = EXCLUDED.updated_at",
            (client_id, calories, protein, fat, carbs, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("КБЖУ сохранены.", reply_markup=_main_menu_markup())
        return MAIN

    conv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin)],
        states={
            MAIN: [
                CallbackQueryHandler(on_schedule_preset, pattern="^adm:schedule:(weekday|saturday)$"),
                CallbackQueryHandler(on_schedule_custom, pattern="^adm:schedule:custom$"),
                CallbackQueryHandler(on_main_menu, pattern="^adm:"),
            ],
            CHOOSE_CLIENT: [
                CallbackQueryHandler(on_client_chosen, pattern=r"^adm:client:\d+$"),
                CallbackQueryHandler(on_main_menu, pattern="^adm:back$"),
            ],
            CLIENT_MENU: [
                CallbackQueryHandler(on_client_chosen, pattern=r"^adm:client:\d+$"),
                CallbackQueryHandler(on_client_menu, pattern="^adm:client:(program|kbju|profile)$"),
                CallbackQueryHandler(on_clients_list, pattern="^adm:clients$"),
                CallbackQueryHandler(on_main_menu, pattern="^adm:back$"),
            ],
            AWAIT_PROGRAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_program_text)],
            AWAIT_KBJU: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_kbju_text)],
            AWAIT_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_custom_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    application.add_handler(conv)
