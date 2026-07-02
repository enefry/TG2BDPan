"""配置模块：从环境变量读取所有配置项"""

import json
import logging
import os
import urllib.parse
from typing import Any
from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)


def _normalize_replacement_netloc(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = value
    if "://" not in parsed:
        parsed = f"//{parsed}"
    parts = urllib.parse.urlsplit(parsed)
    return parts.netloc or parts.path.split("/")[0]


def _parse_domain_replacements(value: str) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        old, sep, new = item.partition("=")
        if not sep:
            continue
        old_netloc = _normalize_replacement_netloc(old)
        new_netloc = _normalize_replacement_netloc(new)
        if old_netloc and new_netloc:
            replacements[old_netloc.lower()] = new_netloc
    return replacements


def _parse_domain_replacements_value(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return _parse_domain_replacements(value)
    if isinstance(value, dict):
        replacements: dict[str, str] = {}
        for old, new in value.items():
            old_netloc = _normalize_replacement_netloc(str(old))
            new_netloc = _normalize_replacement_netloc(str(new))
            if old_netloc and new_netloc:
                replacements[old_netloc.lower()] = new_netloc
        return replacements
    return {}


def _load_json_config(path: str) -> dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _parse_string_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            str(k).strip(): str(v)
            for k, v in value.items()
            if str(k).strip()
        }
    if isinstance(value, list):
        return {str(item).strip(): "" for item in value if str(item).strip()}
    if isinstance(value, str):
        try:
            return _parse_string_map(json.loads(value))
        except json.JSONDecodeError:
            return {item.strip(): "" for item in value.split(",") if item.strip()}
    return {}


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

    # JSON 配置文件。Docker 默认路径对应仓库的 ./data/config.json 挂载。
    CONFIG_FILE: str = os.getenv(
        "CONFIG_FILE", os.path.join(os.getcwd(), "data", "config.json")
    )
    JSON_CONFIG: dict[str, Any] = _load_json_config(CONFIG_FILE)
    CONFIG_FILE_MTIME: float | None = (
        os.path.getmtime(CONFIG_FILE) if os.path.isfile(CONFIG_FILE) else None
    )

    # URL 域名端口替换。环境变量和 JSON 都支持，JSON 中同名规则优先。
    URL_DOMAIN_REPLACEMENTS: dict[str, str] = {
        **_parse_domain_replacements(os.getenv("URL_DOMAIN_REPLACEMENTS", "")),
        **_parse_domain_replacements_value(JSON_CONFIG.get("url_domain_replacements")),
    }

    # yt-dlp cookies 文件路径，用于 Twitter/X 等需要登录态的网站。
    YTDLP_COOKIES_FILE: str = str(
        JSON_CONFIG.get("ytdlp_cookies_file")
        or os.getenv("YTDLP_COOKIES_FILE", "")
    ).strip()
    # yt-dlp 浏览器 cookies 来源，例如 firefox:/data/mozilla/firefox/7u8bnsv1.default-esr
    YTDLP_COOKIES_FROM_BROWSER: str = str(
        JSON_CONFIG.get("ytdlp_cookies_from_browser")
        or os.getenv("YTDLP_COOKIES_FROM_BROWSER", "")
    ).strip()
    # YouTube 使用 cookies 重试时的 player client。mweb 在部分云服务器 IP 上比默认 tv/web 更稳。
    YTDLP_YOUTUBE_PLAYER_CLIENT: str = str(
        JSON_CONFIG.get("ytdlp_youtube_player_client")
        if "ytdlp_youtube_player_client" in JSON_CONFIG
        else os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENT", "mweb")
    ).strip()
    # yt-dlp 格式选择：优先 mp4 视频流 + m4a 音频，其次单文件 mp4，最后回退到任意最佳格式。
    YTDLP_FORMAT: str = str(
        JSON_CONFIG.get("ytdlp_format")
        or os.getenv("YTDLP_FORMAT", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b")
    ).strip()
    # yt-dlp 格式排序：在可选格式中优先 HEVC、H.264、AV1，再比较清晰度等。
    YTDLP_FORMAT_SORT: str = str(
        JSON_CONFIG.get("ytdlp_format_sort")
        or os.getenv("YTDLP_FORMAT_SORT", "vcodec:hevc:h264:av1,res,fps,br")
    ).strip()
    CUSTOM_SENSITIVE_WORDS: dict[str, str] = _parse_string_map(
        JSON_CONFIG.get("custom_sensitive_words")
        or os.getenv("CUSTOM_SENSITIVE_WORDS", "")
    )

    # 日志配置
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "/var/log/pan_saver.log")

    def reload_json_config(self) -> bool:
        """重新加载 JSON 配置中的运行期字段。成功返回 True，失败保留旧配置。"""
        try:
            json_config = _load_json_config(self.CONFIG_FILE)
            mtime = (
                os.path.getmtime(self.CONFIG_FILE)
                if os.path.isfile(self.CONFIG_FILE)
                else None
            )
        except Exception as exc:
            _logger.warning("加载 JSON 配置失败，保留旧配置: %s", exc)
            return False

        self.JSON_CONFIG = json_config
        self.CONFIG_FILE_MTIME = mtime
        self.URL_DOMAIN_REPLACEMENTS = {
            **_parse_domain_replacements(os.getenv("URL_DOMAIN_REPLACEMENTS", "")),
            **_parse_domain_replacements_value(
                json_config.get("url_domain_replacements")
            ),
        }
        self.YTDLP_COOKIES_FILE = str(
            json_config.get("ytdlp_cookies_file")
            or os.getenv("YTDLP_COOKIES_FILE", "")
        ).strip()
        self.YTDLP_COOKIES_FROM_BROWSER = str(
            json_config.get("ytdlp_cookies_from_browser")
            or os.getenv("YTDLP_COOKIES_FROM_BROWSER", "")
        ).strip()
        self.YTDLP_YOUTUBE_PLAYER_CLIENT = str(
            json_config.get("ytdlp_youtube_player_client")
            if "ytdlp_youtube_player_client" in json_config
            else os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENT", "mweb")
        ).strip()
        self.YTDLP_FORMAT = str(
            json_config.get("ytdlp_format")
            or os.getenv(
                "YTDLP_FORMAT",
                "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            )
        ).strip()
        self.YTDLP_FORMAT_SORT = str(
            json_config.get("ytdlp_format_sort")
            or os.getenv("YTDLP_FORMAT_SORT", "vcodec:hevc:h264:av1,res,fps,br")
        ).strip()
        self.CUSTOM_SENSITIVE_WORDS = _parse_string_map(
            json_config.get("custom_sensitive_words")
            or os.getenv("CUSTOM_SENSITIVE_WORDS", "")
        )
        return True

    def reload_json_config_if_changed(self) -> bool:
        mtime = (
            os.path.getmtime(self.CONFIG_FILE)
            if os.path.isfile(self.CONFIG_FILE)
            else None
        )
        if mtime == self.CONFIG_FILE_MTIME:
            return False
        return self.reload_json_config()


config = Config()
