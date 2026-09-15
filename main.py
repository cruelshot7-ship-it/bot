"""
Entrypoint for the bot: booking + program/КБЖУ + your existing commands,
all in one process, one bot token, one database.

>>> ДОБАВЬ СВОИ СУЩЕСТВУЮЩИЕ КОМАНДЫ ЗДЕСЬ <<<
Перенеси все свои текущие application.add_handler(...) из старого bot.py
в функцию main() ниже, ДО строки register_booking_handlers(...).
"""

import asyncio
import os

import uvicorn
from telegram.ext import Application

from booking_api import app as fastapi_app
from booking_handlers import register_booking_handlers, set_booking_menu_button
from nutrition_handlers import register_nutrition_handlers
from progress_handlers import register_progress_handlers
from technique_handlers import register_technique_handlers
from jobs import register_jobs

BOT_TOKEN = os.environ["BOT_TOKEN"]
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])
MINIAPP_URL = os.environ["MINIAPP_URL"]
PORT = int(os.environ.get("PORT", 8000))


async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # >>> СЮДА свои старые application.add_handler(...) <<<

    register_booking_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_nutrition_handlers(application)
    register_progress_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_technique_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_jobs(application, trainer_tg_id=TRAINER_TG_ID)

    await application.initialize()
    await set_booking_menu_button(application, miniapp_url=MINIAPP_URL)
    await application.start()
    await application.updater.start_polling()

    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
