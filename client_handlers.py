
"""Core client UX for the DISCIPLINE bot, backed by the project's Postgres schema."""

from datetime import date, datetime
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from booking_api import get_conn


MENU = [
    ["👤 Профиль", "🏋️ Моя программа"],
    ["🥗 Моё питание", "🔥 Тренировка сегодня"],
    ["📊 Мой прогресс", "📏 Вес и замеры"],
    ["💧 Вода", "✅ Привычки"],
    ["📅 Записаться", "💬 Тренер"],
    ["ℹ️ Помощь"],
]


def keyboard():
    return ReplyKeyboardMarkup(MENU, resize_keyboard=True)


def _ensure_user(update: Update):
    user = update.effective_user
    conn = get_conn()
    conn.execute(
        """INSERT INTO known_users (telegram_id, first_name, username, first_seen)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (telegram_id) DO UPDATE SET
           first_name = EXCLUDED.first_name, username = EXCLUDED.username""",
        (user.id, user.first_name, user.username, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return conn


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    conn.close()
    await update.message.reply_text(
        "🏠 ДИСКИПЛИНА\n\n"
        "Твоя программа, питание, тренировки, прогресс и запись к тренеру в одном месте.",
        reply_markup=keyboard(),
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    row = conn.execute(
        """SELECT u.first_name, u.username, p.weight_kg, p.updated_at
           FROM known_users u
           LEFT JOIN client_profiles p ON p.telegram_id = u.telegram_id
           WHERE u.telegram_id = %s""",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        "👤 ПРОФИЛЬ\n\n"
        f"Имя: {row['first_name'] or '—'}\n"
        f"Username: @{row['username']}" if row["username"] else
        "👤 ПРОФИЛЬ\n\n"
        f"Имя: {row['first_name'] or '—'}"
    )


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT program_text, updated_at FROM client_profiles WHERE telegram_id = %s",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    if not row or not row["program_text"]:
        await update.message.reply_text("🏋️ Программа пока не назначена тренером.")
        return
    await update.message.reply_text(
        f"🏋️ МОЯ ПРОГРАММА\n\n{row['program_text']}"
    )


async def nutrition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    row = conn.execute(
        """SELECT calories, protein, fat, carbs, updated_at
           FROM client_profiles WHERE telegram_id = %s""",
        (update.effective_user.id,),
    ).fetchone()
    today = date.today().isoformat()
    totals = conn.execute(
        """SELECT COALESCE(SUM(calories),0) calories,
                  COALESCE(SUM(protein),0) protein,
                  COALESCE(SUM(fat),0) fat,
                  COALESCE(SUM(carbs),0) carbs
           FROM food_logs WHERE telegram_id = %s AND log_date = %s""",
        (update.effective_user.id, today),
    ).fetchone()
    conn.close()
    if not row or row["calories"] is None:
        await update.message.reply_text("🥗 КБЖУ пока не назначены тренером.")
        return
    await update.message.reply_text(
        "🥗 МОЁ ПИТАНИЕ\n\n"
        f"Цель: {row['calories']} ккал · Б {row['protein']} · Ж {row['fat']} · У {row['carbs']}\n\n"
        f"Сегодня записано: {totals['calories']} ккал · Б {totals['protein']} · "
        f"Ж {totals['fat']} · У {totals['carbs']}"
    )


async def workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT program_text FROM client_profiles WHERE telegram_id = %s",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    if not row or not row["program_text"]:
        await update.message.reply_text("🔥 Тренировка пока не назначена тренером.")
        return
    await update.message.reply_text(
        "🔥 ТРЕНИРОВКА СЕГОДНЯ\n\n"
        "Открой «Моя программа», чтобы выполнить назначенный план.\n\n"
        "Рабочие веса можно фиксировать командой:\n"
        "/log_lift упражнение вес повторения подходы"
    )


async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    rows = conn.execute(
        """SELECT log_date, exercise, weight_kg, reps, sets
           FROM progress_logs WHERE telegram_id = %s
           ORDER BY log_date DESC, created_at DESC LIMIT 10""",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 Записей прогресса пока нет.")
        return
    text = "📊 МОЙ ПРОГРЕСС\n\n"
    text += "\n".join(
        f"• {r['log_date']}: {r['exercise']} — {r['weight_kg']} кг × {r['reps']} × {r['sets']}"
        for r in rows
    )
    await update.message.reply_text(text)


async def measurements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT weight_kg, updated_at FROM client_profiles WHERE telegram_id = %s",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    if not row or row["weight_kg"] is None:
        await update.message.reply_text("📏 Замеров пока нет. Текущий вес можно сохранить через /set_weight.")
        return
    await update.message.reply_text(
        f"📏 ВЕС И ЗАМЕРЫ\n\nТекущий вес: {row['weight_kg']} кг\nОбновлено: {row['updated_at'] or '—'}"
    )


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT ml FROM water_logs WHERE telegram_id = %s AND log_date = %s",
        (update.effective_user.id, today),
    ).fetchone()
    conn.close()
    current = row["ml"] if row else 0
    await update.message.reply_text(
        f"💧 ВОДА\n\nСегодня: {current} мл\n\n"
        "Добавить: /water_add 250"
    )


async def habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT workout, nutrition, sleep, water FROM habit_logs WHERE telegram_id = %s AND log_date = %s",
        (update.effective_user.id, today),
    ).fetchone()
    conn.close()
    row = row or {"workout": 0, "nutrition": 0, "sleep": 0, "water": 0}
    mark = lambda x: "✅" if x else "□"
    await update.message.reply_text(
        f"✅ ПРИВЫЧКИ\n\n"
        f"{mark(row['workout'])} Тренировка\n"
        f"{mark(row['nutrition'])} Питание\n"
        f"{mark(row['sleep'])} Сон\n"
        f"{mark(row['water'])} Вода\n\n"
        "Отметить: /habit тренировка | питание | сон | вода"
    )


async def bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    rows = conn.execute(
        """SELECT s.date, s.time, b.status
           FROM bookings b JOIN slots s ON s.id = b.slot_id
           WHERE b.client_tg_id = %s AND b.status = 'active'
           ORDER BY s.date, s.time""",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📅 Активных записей нет. Нажми «Запись» в меню.")
        return
    await update.message.reply_text(
        "📅 МОИ ЗАПИСИ\n\n" +
        "\n".join(f"• {r['date']} {r['time']}" for r in rows)
    )


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Фото прогресса: отправь фото с подписью «прогресс». "
        "Файловое хранение для фото прогресса требует отдельного object-storage слоя."
    )


async def coach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 СВЯЗЬ С ТРЕНЕРОМ\n\n"
        "Напиши сообщение следующим сообщением. Для автоматической пересылки "
        "сообщений тренеру используется текущий Telegram-контур."
    )


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _ensure_user(update)
    food = conn.execute(
        """SELECT COALESCE(SUM(calories),0) calories, COUNT(*) entries
           FROM food_logs WHERE telegram_id = %s AND log_date >= %s""",
        (update.effective_user.id, (date.today()).isoformat()),
    ).fetchone()
    lifts = conn.execute(
        """SELECT COUNT(*) entries FROM progress_logs
           WHERE telegram_id = %s AND log_date >= %s""",
        (update.effective_user.id, (date.today()).isoformat()),
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        "📊 ОТЧЁТ\n\n"
        f"Записей питания сегодня: {food['entries']}\n"
        f"Калории сегодня: {food['calories']}\n"
        f"Записей рабочих весов сегодня: {lifts['entries']}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ КОМАНДЫ\n\n"
        "/start /profile /plan /nutrition /workout /report /progress "
        "/measurements /photo /water /habits /bookings /coach /help\n\n"
        "Вода: /water_add 250\n"
        "Прогресс: /log_lift упражнение вес повторения подходы"
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = {
        "👤 Профиль": profile,
        "🏋️ Моя программа": plan,
        "🥗 Моё питание": nutrition,
        "🔥 Тренировка сегодня": workout,
        "📊 Мой прогресс": progress,
        "📏 Вес и замеры": measurements,
        "💧 Вода": water,
        "✅ Привычки": habits,
        "📅 Записаться": bookings,
        "💬 Тренер": coach,
        "ℹ️ Помощь": help_cmd,
    }
    fn = routes.get(update.message.text)
    if fn:
        await fn(update, context)


async def water_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ml = int(context.args[0])
        if ml <= 0 or ml > 5000:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Формат: /water_add 250")
        return
    conn = _ensure_user(update)
    today = date.today().isoformat()
    conn.execute(
        """INSERT INTO water_logs (telegram_id, log_date, ml)
           VALUES (%s, %s, %s)
           ON CONFLICT (telegram_id, log_date) DO UPDATE SET ml = water_logs.ml + EXCLUDED.ml""",
        (update.effective_user.id, today, ml),
    )
    conn.commit()
    row = conn.execute(
        "SELECT ml FROM water_logs WHERE telegram_id = %s AND log_date = %s",
        (update.effective_user.id, today),
    ).fetchone()
    conn.close()
    await update.message.reply_text(f"💧 Добавлено {ml} мл. Сегодня: {row['ml']} мл.")


async def habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed = {"тренировка": "workout", "питание": "nutrition", "сон": "sleep", "вода": "water"}
    key = " ".join(context.args).strip().lower()
    if key not in allowed:
        await update.message.reply_text("Формат: /habit тренировка | питание | сон | вода")
        return
    conn = _ensure_user(update)
    today = date.today().isoformat()
    conn.execute(
        """INSERT INTO habit_logs (telegram_id, log_date, workout, nutrition, sleep, water)
           VALUES (%s,%s,0,0,0,0)
           ON CONFLICT (telegram_id, log_date) DO NOTHING""",
        (update.effective_user.id, today),
    )
    conn.execute(
        f"UPDATE habit_logs SET {allowed[key]} = 1 WHERE telegram_id = %s AND log_date = %s",
        (update.effective_user.id, today),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Отмечено: {key}")


def register_client_handlers(application: Application):
    pairs = {
        "start": start, "profile": profile, "plan": plan, "nutrition": nutrition,
        "workout": workout, "report": report, "progress": progress,
        "measurements": measurements, "photo": photo, "water": water,
        "habits": habits, "bookings": bookings, "coach": coach, "help": help_cmd,
        "water_add": water_add, "habit": habit,
    }
    for command, fn in pairs.items():
        application.add_handler(CommandHandler(command, fn))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
