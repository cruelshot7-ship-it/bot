"""
Entrypoint: runs the bot (polling) and the booking API (FastAPI) in one
process, one bot token, one database.

Includes a supervisor that watches the Telegram polling loop and
restarts it automatically if it dies — e.g. from a brief Conflict error
during a Railway rolling redeploy, where the old and new deployment can
overlap for a few seconds. Without this, the API (uvicorn) keeps
answering fine while the bot goes silently dead until someone manually
restarts the service.

FIX (2026-09-17): the very first application.updater.start_polling()
call used to be awaited directly, unguarded. If a Conflict happened on
that first attempt (old + new container overlapping during a rolling
redeploy), the exception propagated out of main() and killed the whole
process — including uvicorn, which is why the API went down too and
every client-facing request (including bookings) failed until Railway
finished restarting the container. Both the initial start and every
restart from the supervisor now go through _start_polling_safely(),
which retries with backoff instead of raising.

>>> ДОБАВЬ СВОИ СУЩЕСТВУЮЩИЕ КОМАНДЫ ЗДЕСЬ <<<
Если хочешь сохранить команды старого бота — вставь их
application.add_handler(...) в main() ниже, до строки
register_booking_handlers(...).
"""
import asyncio
import logging
import os

import uvicorn
from telegram.ext import Application

from booking_api import app as fastapi_app
from booking_handlers import register_booking_handlers, set_booking_menu_button
from nutrition_handlers import register_nutrition_handlers
from progress_handlers import register_progress_handlers
from technique_handlers import register_technique_handlers
from schedule_handlers import register_schedule_handlers
from admin_handlers import register_admin_handlers
from jobs import register_jobs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

BOT_TOKEN = os.environ["BOT_TOKEN"]
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])
MINIAPP_URL = os.environ["MINIAPP_URL"]
PORT = int(os.environ.get("PORT", 8000))


async def _error_handler(update, context):
    logger.error("Unhandled error while processing update: %s", context.error)


async def _start_polling_safely(application: Application, max_backoff: int = 30):
    """Starts (or restarts) polling, retrying with backoff instead of
    raising. Covers the case where a Conflict fires on the very first
    attempt (old + new container overlapping during a Railway rolling
    redeploy) — that used to crash the whole process before the
    supervisor loop even got a chance to run."""
    backoff = 3
    while True:
        try:
            await application.updater.start_polling(drop_pending_updates=True)
            return
        except Exception:
            logger.exception("start_polling failed — retrying in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def _polling_supervisor(application: Application):
    """Checks every 15s whether polling is still running; restarts it if
    not. A brief Conflict during a redeploy overlap self-heals within one
    check instead of leaving the bot dead until a manual restart."""
    while True:
        await asyncio.sleep(15)
        if not application.updater.running:
            logger.warning("Polling is not running — attempting to restart it")
            await _start_polling_safely(application)
            logger.info("Polling restarted successfully")


async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # >>> СЮДА свои старые application.add_handler(...) <<<

    register_booking_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_nutrition_handlers(application)
    register_progress_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_technique_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_schedule_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_admin_handlers(application, trainer_tg_id=TRAINER_TG_ID)
    register_jobs(application, trainer_tg_id=TRAINER_TG_ID)
    application.add_error_handler(_error_handler)

    await application.initialize()
    await set_booking_menu_button(application, miniapp_url=MINIAPP_URL)
    await application.start()
    await _start_polling_safely(application)
    asyncio.create_task(_polling_supervisor(application))

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
