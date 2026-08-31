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
from app.rewards_routes import router as rewards_router
from app.web import create_web_app


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(rewards_router)

    await init_db()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="✨ Главное меню"),
            BotCommand(command="paysupport", description="💬 Написать в поддержку"),
            BotCommand(command="id", description="🪪 Показать Telegram ID"),
            BotCommand(command="admin", description="⚙️ Управление магазином"),
            BotCommand(command="bonus", description="💎 Бонусы"),
            BotCommand(command="referral", description="👥 Рефералы"),
            BotCommand(command="promo", description="🎁 Промокод"),
        ]
    )

    web_app = create_web_app(bot)
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
