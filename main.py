import asyncio
import logging
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import uvicorn
from telegram.ext import Application

from booking_api import app as fastapi_app
from booking_handlers import register_booking_handlers, set_booking_menu_button
from client_handlers import register_client_handlers
from nutrition_handlers import register_nutrition_handlers
from progress_handlers import register_progress_handlers
from technique_handlers import register_technique_handlers
from schedule_handlers import register_schedule_handlers
from admin_handlers import register_admin_handlers
from jobs import register_jobs

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discipline")

BOT_TOKEN = os.environ["BOT_TOKEN"]
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])
MINIAPP_URL = os.environ["MINIAPP_URL"]
PORT = int(os.getenv("PORT", "8000"))


async def error_handler(update, context):
    logger.exception("Telegram handler error", exc_info=context.error)


async def start_polling_with_retry(application):
    delay = 3
    while True:
        try:
            await application.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=None,
            )
            return
        except Exception:
            logger.exception("Polling start failed; retrying in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def cache_bust_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["v"] = os.getenv("APP_VERSION", "20260920-1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # /start belongs only to client_handlers. No duplicate /start handler.
    register_client_handlers(application)
    register_booking_handlers(application, TRAINER_TG_ID)
    register_nutrition_handlers(application)
    register_progress_handlers(application, TRAINER_TG_ID)
    register_technique_handlers(application, TRAINER_TG_ID)
    register_schedule_handlers(application, TRAINER_TG_ID)
    register_admin_handlers(application, TRAINER_TG_ID)
    register_jobs(application, TRAINER_TG_ID)
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()

    try:
        await set_booking_menu_button(application, cache_bust_url(MINIAPP_URL))
        logger.info("Telegram Mini App menu configured: %s", cache_bust_url(MINIAPP_URL))
    except Exception:
        logger.exception("Could not configure Telegram Mini App menu button; bot will continue")

    await start_polling_with_retry(application)

    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
        )
    )
    try:
        await server.serve()
    finally:
        if application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
