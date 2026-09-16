"""
Photo-based calorie logging + activity calorie-burn calculator.

ENV VAR REQUIRED (in addition to the booking ones):
    ANTHROPIC_API_KEY - from console.anthropic.com. Separate account/
    billing from any claude.ai chat subscription — pay-per-use, no
    monthly fee. A food photo estimate costs roughly $0.003-0.005 at
    current Sonnet 5 rates.

Register with:
    from nutrition_handlers import register_nutrition_handlers
    register_nutrition_handlers(application)
"""

import base64
import json
import os
from datetime import datetime, date

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from anthropic import AsyncAnthropic

from booking_api import get_conn

anthropic_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# MET (metabolic equivalent) values — standard reference table, kept short
# on purpose. Add more rows if your clients do other activities.
MET_TABLE = {
    "бег": 9.8, "ходьба": 3.5, "велосипед": 7.5, "плавание": 8.0,
    "силовая": 6.0, "йога": 2.5, "кроссфит": 8.0, "гребля": 7.0,
}


async def _estimate_food(image_bytes: bytes, media_type: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    response = await anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": (
                    "Оцени калорийность и БЖУ еды на фото. Ответь СТРОГО в JSON, без пояснений и "
                    'без markdown-разметки: {"description": "краткое название блюда", '
                    '"calories": число, "protein": число, "fat": число, "carbs": число}'
                )},
            ],
        }],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def register_nutrition_handlers(application: Application):
    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = await tg_file.download_as_bytearray()

        thinking_msg = await update.message.reply_text("Считаю калории по фото...")
        try:
            est = await _estimate_food(bytes(image_bytes), "image/jpeg")
        except Exception:
            await thinking_msg.edit_text(
                "Не получилось распознать еду на фото. Попробуй снять крупным планом при хорошем свете."
            )
            return

        conn = get_conn()
        today = date.today().isoformat()
        log_id = f"food_{user.id}_{int(datetime.utcnow().timestamp())}"
        conn.execute(
            "INSERT INTO food_logs (id, telegram_id, log_date, description, calories, protein, fat, carbs, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (log_id, user.id, today, est.get("description"), est.get("calories"),
             est.get("protein"), est.get("fat"), est.get("carbs"), datetime.utcnow().isoformat()),
        )
        conn.commit()
        totals = conn.execute(
            "SELECT COALESCE(SUM(calories),0) AS calories, COALESCE(SUM(protein),0) AS protein, "
            "COALESCE(SUM(fat),0) AS fat, COALESCE(SUM(carbs),0) AS carbs "
            "FROM food_logs WHERE telegram_id = %s AND log_date = %s",
            (user.id, today),
        ).fetchone()
        conn.close()

        await thinking_msg.edit_text(
            f"{est.get('description', 'Блюдо')}: ~{est.get('calories', '?')} ккал "
            f"(Б {est.get('protein', '?')} / Ж {est.get('fat', '?')} / У {est.get('carbs', '?')})\n\n"
            f"Итого за сегодня: {totals['calories']} ккал "
            f"(Б {totals['protein']} / Ж {totals['fat']} / У {totals['carbs']})\n\n"
            f"Это оценка ИИ по фото, не аптечная точность — для точных цифр взвешивай порции."
        )

    async def set_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            weight = float(context.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("Формат: /set_weight 78")
            return
        conn = get_conn()
        conn.execute(
            "INSERT INTO client_profiles (telegram_id, weight_kg, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (telegram_id) DO UPDATE SET weight_kg = EXCLUDED.weight_kg, "
            "updated_at = EXCLUDED.updated_at",
            (update.effective_user.id, weight, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Вес сохранён: {weight} кг")

    async def activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) < 2:
            await update.message.reply_text(
                "Формат: /activity <тип> <минуты>\n"
                f"Доступные типы: {', '.join(MET_TABLE.keys())}\n"
                "Пример: /activity бег 30"
            )
            return
        activity_type = context.args[0].lower()
        try:
            minutes = float(context.args[1])
        except ValueError:
            await update.message.reply_text("Минуты должны быть числом.")
            return
        if activity_type not in MET_TABLE:
            await update.message.reply_text(f"Не знаю такой тип. Доступные: {', '.join(MET_TABLE.keys())}")
            return

        conn = get_conn()
        row = conn.execute(
            "SELECT weight_kg FROM client_profiles WHERE telegram_id = %s",
            (update.effective_user.id,),
        ).fetchone()
        conn.close()
        if not row or not row["weight_kg"]:
            await update.message.reply_text("Сначала укажи вес: /set_weight 78")
            return

        kcal = round(MET_TABLE[activity_type] * row["weight_kg"] * (minutes / 60))
        await update.message.reply_text(f"{activity_type}, {minutes} мин: сожжено ~{kcal} ккал")

    application.add_handler(CommandHandler("set_weight", set_weight))
    application.add_handler(CommandHandler("activity", activity))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
