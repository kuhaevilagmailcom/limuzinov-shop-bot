from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.config import get_settings
from app.db import init_db
from app.handlers import router
from app.web import create_web_app


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await init_db()
    me = await bot.get_me()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть магазин"),
            BotCommand(command="paysupport", description="Помощь с оплатой"),
        ]
    )

    web_app = create_web_app(bot, me.username or "")
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
    )

    try:
        await asyncio.gather(
            dp.start_polling(bot),
            server.serve(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
