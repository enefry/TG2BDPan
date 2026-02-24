import asyncio
from loguru import logger
from telegram.ext import ApplicationBuilder
import bot
import db
import baidu_pan
from config import config


async def background_refresh_task():
    """后台任务，每 12 小时循环所有用户并触发 token 的续期检测"""
    while True:
        try:
            logger.info("后台刷新检测任务启动")
            users = await db.get_all_users()
            for uid in users:
                await baidu_pan.get_valid_token(uid)
        except Exception as e:
            logger.exception("后台定期刷新出错")

        await asyncio.sleep(43200)  # 12 小时


async def main():
    # 配置 loguru 日志
    logger.add(
        config.LOG_FILE,
        rotation="10 MB",
        retention="30 days",
        level=config.LOG_LEVEL,
        encoding="utf-8",
        enqueue=True,
    )

    logger.info("初始化数据库...")
    await db.init_db()

    logger.info("启动 Telegram Bot...")
    builder = ApplicationBuilder().token(config.BOT_TOKEN)
    if config.USE_LOCAL_API:
        logger.info(f"开启 Local Bot API 支持: {config.TG_API_BASE_URL}")
        # file_url 会将 "/bot" 替换为 "/file/bot"
        file_url = config.TG_API_BASE_URL.replace("/bot", "/file/bot")
        builder = (
            builder.base_url(config.TG_API_BASE_URL)
            .base_file_url(file_url)
            .local_mode(True)
        )

    application = builder.build()

    bot.register_handlers(application)

    logger.info("Bot 已启动，开始监听消息")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # 启动后台刷新任务
    refresh_task = asyncio.create_task(background_refresh_task())

    # 保持运行
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        refresh_task.cancel()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已退出")
