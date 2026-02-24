"""配置模块：从环境变量读取所有配置项"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    BOT_TOKEN: str = os.environ["BOT_TOKEN"]
    USE_LOCAL_API: bool = str(os.getenv("USE_LOCAL_API", "false")).lower() == "true"
    TG_API_BASE_URL: str = os.getenv(
        "TG_API_BASE_URL", "http://telegram-bot-api:8081/bot"
    )

    # 允许的用户 ID 列表（逗号分隔），为空则不限制
    ALLOWED_USER_IDS: set[int] = set(
        int(x.strip())
        for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if x.strip()
    )

    # 百度网盘 OpenAPI
    BAIDU_APP_KEY: str = os.environ["BAIDU_APP_KEY"]
    BAIDU_SECRET_KEY: str = os.environ["BAIDU_SECRET_KEY"]
    # OAuth 回调地址（必须与开放平台配置一致，设置为 oob 表示本地授权）
    BAIDU_REDIRECT_URI: str = os.getenv("BAIDU_REDIRECT_URI", "oob")

    # 百度网盘保存目录（以 / 开头）
    BAIDU_SAVE_PATH: str = os.getenv("BAIDU_SAVE_PATH", "/telegram_saves")

    # SQLite 数据库路径
    DB_PATH: str = os.getenv("DB_PATH", "/app/data/pan_saver.db")

    # 临时下载目录
    TMP_DIR: str = os.getenv("TMP_DIR", "/tmp/pan_saver")

    # 日志配置
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "/app/data/pan_saver.log")


config = Config()
