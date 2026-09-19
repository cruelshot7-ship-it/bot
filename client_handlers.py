from datetime import date, timedelta, datetime

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


def _ensure_user(update):
    user = update.effective_user
    conn = get_conn()
    conn.execute(
        """INSERT INTO known_users
           (telegram_id,first_name,username,first_seen)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(telegram_id) DO UPDATE SET
           first_name=EXCLUDED.first_name,username=EXCLUDED.username""",
        (user.id, user.first_name, user.username, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return conn


async def start(update, context):
    conn = _ensure_user(update)
    conn.close()
    await update.message.reply_text(
        "🏠 ДИСКИПЛИНА\n\n"
        "Программа, питание, тренировки, прогресс и запись к тренеру.",
        reply_markup=keyboard(),
    )


async def profile(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        """SELECT u.first_name,u.username,p.weight_kg,p.updated_at
           FROM known_users u LEFT JOIN client_profiles p
           ON p.telegram_id=u.telegram_id WHERE u.telegram_id=%s""",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    username = f"@{row['username']}" if row and row["username"] else "—"
    await update.message.reply_text(
        f"👤 ПРОФИЛЬ\n\nИмя: {row['first_name'] or '—'}\n"
        f"Username: {username}\n"
        f"Вес: {row['weight_kg'] if row and row['weight_kg'] is not None else '—'} кг"
    )


async def plan(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT program_text,updated_at FROM client_profiles WHERE telegram_id=%s",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        f"🏋️ МОЯ ПРОГРАММА\n\n{row['program_text'] if row and row['program_text'] else 'Программа пока не назначена.'}"
    )


async def nutrition(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT calories,protein,fat,carbs FROM client_profiles WHERE telegram_id=%s",
        (update.effective_user.id,),
    ).fetchone()
    totals = conn.execute(
        """SELECT COALESCE(SUM(calories),0) calories,
                  COALESCE(SUM(protein),0) protein,
                  COALESCE(SUM(fat),0) fat,
                  COALESCE(SUM(carbs),0) carbs
           FROM food_logs WHERE telegram_id=%s AND log_date=%s""",
        (update.effective_user.id, date.today().isoformat()),
    ).fetchone()
    conn.close()
    if not row or row["calories"] is None:
        await update.message.reply_text("🥗 КБЖУ пока не назначены тренером.")
        return
    await update.message.reply_text(
        f"🥗 МОЁ ПИТАНИЕ\n\n"
        f"Цель: {row['calories']} ккал · Б {row['protein']} · Ж {row['fat']} · У {row['carbs']}\n"
        f"Сегодня: {totals['calories']} ккал · Б {totals['protein']} · "
        f"Ж {totals['fat']} · У {totals['carbs']}"
    )


async def workout(update, context):
    await plan(update, context)


async def progress(update, context):
    conn = _ensure_user(update)
    rows = conn.execute(
        """SELECT log_date,exercise,weight_kg,reps,sets FROM progress_logs
           WHERE telegram_id=%s ORDER BY created_at DESC LIMIT 10""",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 Записей прогресса пока нет.")
        return
    await update.message.reply_text(
        "📊 МОЙ ПРОГРЕСС\n\n" + "\n".join(
            f"• {r['log_date']}: {r['exercise']} — {r['weight_kg']} кг × {r['reps']} × {r['sets']}"
            for r in rows
        )
    )


async def measurements(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT weight_kg,updated_at FROM client_profiles WHERE telegram_id=%s",
        (update.effective_user.id,),
    ).fetchone()
    conn.close()
    if not row or row["weight_kg"] is None:
        await update.message.reply_text("📏 Вес не задан. Используй /set_weight 90")
        return
    await update.message.reply_text(
        f"📏 ВЕС И ЗАМЕРЫ\n\nТекущий вес: {row['weight_kg']} кг\n"
        f"Обновлено: {row['updated_at'] or '—'}"
    )


async def water(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        "SELECT ml FROM water_logs WHERE telegram_id=%s AND log_date=%s",
        (update.effective_user.id, date.today().isoformat()),
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        f"💧 ВОДА\n\nСегодня: {row['ml'] if row else 0} мл\n\n/water_add 250"
    )


async def habits(update, context):
    conn = _ensure_user(update)
    row = conn.execute(
        """SELECT workout,nutrition,sleep,water FROM habit_logs
           WHERE telegram_id=%s AND log_date=%s""",
        (update.effective_user.id, date.today().isoformat()),
    ).fetchone()
    conn.close()
    row = row or {"workout": 0, "nutrition": 0, "sleep": 0, "water": 0}
    mark = lambda v: "✅" if v else "□"
    await update.message.reply_text(
        "✅ ПРИВЫЧКИ\n\n"
        f"{mark(row['workout'])} Тренировка\n"
        f"{mark(row['nutrition'])} Питание\n"
        f"{mark(row['sleep'])} Сон\n"
        f"{mark(row['water'])} Вода\n\n"
        "/habit тренировка\n/habit питание\n/habit сон\n/habit вода"
    )


async def bookings(update, context):
    conn = _ensure_user(update)
    rows = conn.execute(
        """SELECT s.date,s.time FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE b.client_tg_id=%s AND b.status='active' ORDER BY s.date,s.time""",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    await update.message.reply_text(
        "📅 МОИ ЗАПИСИ\n\n" +
        ("\n".join(f"• {r['date']} {r['time']}" for r in rows) if rows else "Активных записей нет.")
    )


async def coach(update, context):
    context.user_data["coach_mode"] = True
    await update.message.reply_text("💬 Напиши следующее сообщение, я передам его тренеру.")


async def report(update, context):
    start = date.today() - timedelta(days=6)
    conn = _ensure_user(update)
    food = conn.execute(
        """SELECT COALESCE(SUM(calories),0) calories,COUNT(*) entries
           FROM food_logs WHERE telegram_id=%s AND log_date >= %s""",
        (update.effective_user.id, start.isoformat()),
    ).fetchone()
    lifts = conn.execute(
        """SELECT COUNT(*) entries FROM progress_logs
           WHERE telegram_id=%s AND log_date >= %s""",
        (update.effective_user.id, start.isoformat()),
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 ОТЧЁТ ЗА 7 ДНЕЙ\n\n"
        f"Записей питания: {food['entries']}\n"
        f"Калории всего: {food['calories']}\n"
        f"Рабочих весов: {lifts['entries']}"
    )


async def help_cmd(update, context):
    await update.message.reply_text(
        "ℹ️ КОМАНДЫ\n\n"
        "/profile /plan /nutrition /workout /report /progress /measurements\n"
        "/photo /water /habits /bookings /coach /help\n"
        "/set_weight 90\n/water_add 250\n/habit тренировка\n"
        "/log_lift жим 80 8 4"
    )


async def water_add(update, context):
    try:
        ml = int(context.args[0])
        if not 1 <= ml <= 5000:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Формат: /water_add 250")
        return
    conn = _ensure_user(update)
    today = date.today().isoformat()
    conn.execute(
        """INSERT INTO water_logs(telegram_id,log_date,ml) VALUES(%s,%s,%s)
           ON CONFLICT(telegram_id,log_date) DO UPDATE
           SET ml=water_logs.ml+EXCLUDED.ml""",
        (update.effective_user.id, today, ml),
    )
    conn.commit()
    total = conn.execute(
        "SELECT ml FROM water_logs WHERE telegram_id=%s AND log_date=%s",
        (update.effective_user.id, today),
    ).fetchone()["ml"]
    conn.close()
    await update.message.reply_text(f"💧 Добавлено {ml} мл. Сегодня: {total} мл.")


async def habit(update, context):
    allowed = {
        "тренировка": "workout",
        "питание": "nutrition",
        "сон": "sleep",
        "вода": "water",
    }
    key = " ".join(context.args).strip().lower()
    if key not in allowed:
        await update.message.reply_text("Формат: /habit тренировка | питание | сон | вода")
        return
    conn = _ensure_user(update)
    today = date.today().isoformat()
    conn.execute(
        """INSERT INTO habit_logs(telegram_id,log_date)
           VALUES(%s,%s) ON CONFLICT DO NOTHING""",
        (update.effective_user.id, today),
    )
    conn.execute(
        f"UPDATE habit_logs SET {allowed[key]}=1 WHERE telegram_id=%s AND log_date=%s",
        (update.effective_user.id, today),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Отмечено: {key}")


async def photo(update, context):
    await update.message.reply_text("📸 Отправь фото еды без команды, бот попробует оценить КБЖУ.")


async def text_router(update, context):
    if context.user_data.get("coach_mode"):
        context.user_data["coach_mode"] = False
        try:
            await context.bot.send_message(
                int(os.environ["TRAINER_TG_ID"]),
                f"💬 Сообщение от {update.effective_user.first_name or 'клиента'} "
                f"(id {update.effective_user.id}):\n\n{update.message.text}",
            )
            await update.message.reply_text("Передано тренеру.")
        except Exception:
            await update.message.reply_text("Не удалось передать сообщение тренеру.")
        return

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


def register_client_handlers(application: Application):
    commands = {
        "start": start,
        "profile": profile,
        "plan": plan,
        "nutrition": nutrition,
        "workout": workout,
        "report": report,
        "progress": progress,
        "measurements": measurements,
        "photo": photo,
        "water": water,
        "habits": habits,
        "bookings": bookings,
        "coach": coach,
        "help": help_cmd,
        "water_add": water_add,
        "habit": habit,
    }
    for name, fn in commands.items():
        application.add_handler(CommandHandler(name, fn))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
