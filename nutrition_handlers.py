import base64
import json
import os
import re
import time
from datetime import datetime, date

from anthropic import AsyncAnthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from booking_api import get_conn

MET_TABLE = {
    "бег": 9.8, "ходьба": 3.5, "велосипед": 7.5, "плавание": 8.0,
    "силовая": 6.0, "йога": 2.5, "кроссфит": 8.0, "гребля": 7.0,
}


def _client():
    key = os.getenv("ANTHROPIC_API_KEY")
    return AsyncAnthropic(api_key=key) if key else None


def _clean_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


async def _estimate_food(image_bytes, media_type):
    client = _client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    b64 = base64.b64encode(image_bytes).decode()
    response = await client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Оцени еду на фото. Верни строго JSON без markdown: "
                        '{"description":"...", "calories":0, "protein":0, "fat":0, "carbs":0}. '
                        "Все числовые значения должны быть неотрицательными."
                    ),
                },
            ],
        }],
    )
    data = _clean_json(response.content[0].text)
    required = ("description", "calories", "protein", "fat", "carbs")
    if any(k not in data for k in required):
        raise ValueError("AI JSON missing fields")
    result = {
        "description": str(data["description"])[:500],
        "calories": int(data["calories"]),
        "protein": int(data["protein"]),
        "fat": int(data["fat"]),
        "carbs": int(data["carbs"]),
    }
    if any(v < 0 for v in result.values() if isinstance(v, int)):
        raise ValueError("negative nutrition value")
    return result


def register_nutrition_handlers(application: Application):
    async def photo_handler(update, context):
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = await tg_file.download_as_bytearray()
        msg = await update.message.reply_text("Считаю КБЖУ по фото...")
        try:
            est = await _estimate_food(bytes(image_bytes), "image/jpeg")
        except Exception:
            await msg.edit_text(
                "Не получилось распознать еду. Проверь фото и наличие ANTHROPIC_API_KEY."
            )
            return

        user = update.effective_user
        now = datetime.utcnow()
        log_id = f"food_{user.id}_{time.time_ns()}"
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO food_logs
                   (id,telegram_id,log_date,description,calories,protein,fat,carbs,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    log_id, user.id, date.today().isoformat(),
                    est["description"], est["calories"], est["protein"],
                    est["fat"], est["carbs"], now.isoformat(),
                ),
            )
            conn.commit()
            totals = conn.execute(
                """SELECT COALESCE(SUM(calories),0) calories,
                          COALESCE(SUM(protein),0) protein,
                          COALESCE(SUM(fat),0) fat,
                          COALESCE(SUM(carbs),0) carbs
                   FROM food_logs WHERE telegram_id=%s AND log_date=%s""",
                (user.id, date.today().isoformat()),
            ).fetchone()
        finally:
            conn.close()

        await msg.edit_text(
            f"{est['description']}: ~{est['calories']} ккал "
            f"(Б {est['protein']} / Ж {est['fat']} / У {est['carbs']})\n\n"
            f"Итого сегодня: {totals['calories']} ккал "
            f"(Б {totals['protein']} / Ж {totals['fat']} / У {totals['carbs']})\n\n"
            "Оценка ИИ приблизительная."
        )

    async def set_weight(update, context):
        try:
            weight = float(context.args[0])
            if not 30 <= weight <= 300:
                raise ValueError
        except (IndexError, ValueError):
            await update.message.reply_text("/set_weight 90")
            return
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO client_profiles(telegram_id,weight_kg,updated_at)
                   VALUES(%s,%s,%s)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   weight_kg=EXCLUDED.weight_kg,updated_at=EXCLUDED.updated_at""",
                (update.effective_user.id, weight, datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await update.message.reply_text(f"Вес сохранён: {weight:g} кг")

    async def activity(update, context):
        if len(context.args) != 2 or context.args[0].lower() not in MET_TABLE:
            await update.message.reply_text(
                "/activity <тип> <минуты>\nТипы: " + ", ".join(MET_TABLE)
            )
            return
        try:
            minutes = float(context.args[1])
            if not 1 <= minutes <= 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Минуты: число от 1 до 1000.")
            return
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT weight_kg FROM client_profiles WHERE telegram_id=%s",
                (update.effective_user.id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or row["weight_kg"] is None:
            await update.message.reply_text("Сначала /set_weight 90")
            return
        kcal = round(MET_TABLE[context.args[0].lower()] * row["weight_kg"] * minutes / 60)
        await update.message.reply_text(f"Расход: примерно {kcal} ккал.")


    application.add_handler(CommandHandler("set_weight", set_weight))
    application.add_handler(CommandHandler("activity", activity))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
