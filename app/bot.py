"""Telegram Bot 主逻辑"""

import asyncio
import logging
import os
import re
import time
import urllib.parse
from typing import Optional, Any
from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from loguru import logger

import baidu_pan, db
from config import config
from downloader import download_telegram_file, download_url

# ─── 白名单检查 ─────────────────────────────────────────────────────────────────


def _is_allowed(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


# ─── 辅助函数 ───────────────────────────────────────────────────────────────────

URL_REGEX = re.compile(r"https?://[^\s]+")


def _extract_urls(text: str) -> list[str]:
    return URL_REGEX.findall(text)


def _extract_baidu_code(text: str) -> Optional[str]:
    """从文本或 URL 中提取百度 OAuth code，兼容 OOB 界面的纯文本授权码"""
    text = text.strip()

    # 1. 如果是对整个链接的匹配，尝试作为 URL 解析提取 query
    try:
        parsed = urllib.parse.urlparse(text)
        if parsed.query or parsed.fragment:
            # 可能是 ?code=xxx 或者 #code=xxx
            query_qs = urllib.parse.parse_qs(parsed.query)
            if "code" in query_qs:
                return query_qs["code"][0]
            fragment_qs = urllib.parse.parse_qs(parsed.fragment)
            if "code" in fragment_qs:
                return fragment_qs["code"][0]
    except Exception:
        pass

    # 2. 如果包含 code= ，直接正则提取
    m = re.search(r"code=([^&\s]+)", text)
    if m:
        return m.group(1)

    # 3. 可能是用户直接复制的 32 位 OOB 纯字符串授权码
    if len(text) >= 30 and re.match(r"^[A-Za-z0-9]+$", text):
        return text

    return None


class ProgressNotifier:
    """防刷屏的进度通知器"""

    def __init__(self, msg: Message, prefix: str):
        self.msg = msg
        self.prefix = prefix
        self.last_update_time: float = 0
        self.last_text: str = ""

    async def __call__(self, current: int, total: int) -> None:
        now = time.time()
        # 控制更新频率：至少间隔 1.5 秒
        if now - self.last_update_time < 1.5 and current < total:
            return

        percentage = (current / total * 100) if total > 0 else 0
        current_mb = current / 1024 / 1024
        total_mb = total / 1024 / 1024

        text = f"{self.prefix}\n`{current_mb:.2f} MB / {total_mb:.2f} MB ({percentage:.1f}%)`"
        if text == self.last_text:
            return

        try:
            await self.msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            self.last_text = text
            self.last_update_time = now
        except Exception as e:
            # 忽略 Message is not modified 等错误
            pass


async def _do_transfer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    local_path: str,
    filename: str,
) -> None:
    """上传本地文件到百度网盘并回复结果，最后清理临时文件"""
    if not update.effective_user or not update.message:
        return

    user_id: int = update.effective_user.id
    try:
        msg: Message = await update.message.reply_text(
            f"⏫ 准备转存 `{filename}` 到百度网盘…", parse_mode=ParseMode.MARKDOWN_V2
        )
        notifier = ProgressNotifier(msg, f"⏫ 正在上传 `{filename}`")

        max_retries = 3
        retry_delay = 5
        result: dict[str, Any] = {}

        for attempt in range(1, max_retries + 1):
            try:
                result = await baidu_pan.upload_file(
                    user_id, local_path, filename, progress_cb=notifier
                )
                break
            except Exception as e:
                logger.exception(
                    f"上传百度网盘异常: {filename}, 第 {attempt}/{max_retries} 次尝试"
                )
                if attempt < max_retries:
                    try:
                        await msg.edit_text(
                            f"⚠️ 转存异常，{retry_delay} 秒后重试 ({attempt}/{max_retries})：\n{str(e)[:100]}"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(retry_delay)
                else:
                    try:
                        await msg.edit_text(
                            f"❌ 最终转存失败，已重试 {max_retries} 次：\n{str(e)[:200]}"
                        )
                    except Exception:
                        pass
                    return

        if result["ok"]:
            await msg.edit_text(
                f"✅ 转存成功！\n📁 保存路径：`{result['path']}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await msg.edit_text(f"❌ 转存失败：{result.get('error', 'Unknown')}")
    finally:
        if local_path and os.path.isfile(local_path):
            os.remove(local_path)


# ─── 命令处理器 ─────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "👋 *欢迎使用百度网盘转存 Bot*\n\n"
        "📌 *支持的操作：*\n"
        "• 发送任意文件 → 自动转存到百度网盘\n"
        "• 发送文件直链 URL → 下载后转存\n"
        "• 发送视频平台链接（B站、YouTube 等）→ 下载后转存\n\n"
        "⚙️ *命令：*\n"
        "/auth \\- 授权百度网盘\n"
        "/status \\- 查看授权状态\n"
        "/reauth \\- 重新授权"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        return
    auth_url = baidu_pan.get_auth_url()
    text = (
        "🔑 *百度网盘授权*\n\n"
        f"1\\. 点击链接完成授权：[点我授权]({auth_url})\n"
        "2\\. 授权成功后，浏览器会跳转到一个页面\n"
        "3\\. 将*浏览器地址栏中的完整 URL* 复制并发送给我\n"
        "\\(URL 中包含 `code=` 参数，我会自动识别\\)"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    record = await db.get_token(user_id)
    if not record:
        await update.message.reply_text("❌ 尚未授权百度网盘，请发送 /auth")
        return

    # 若名字“未知”，主动去获取一次百度网盘信息
    baidu_name = record.get("baidu_name")
    if not baidu_name:
        uinfo = await baidu_pan.get_uinfo(user_id, record["access_token"])
        if uinfo and "baidu_name" in uinfo:
            baidu_name = uinfo["baidu_name"]
            # 不要忘了同步回数据库里存下来
            await db.save_token(
                user_id=user_id,
                access_token=record["access_token"],
                refresh_token=record["refresh_token"],
                expires_in=record["expires_at"] - int(time.time()),
                baidu_name=baidu_name,
            )

    remaining = record["expires_at"] - int(time.time())
    baidu_name_text = f"👤当前绑定账号：`{baidu_name or '未知'}`\n"

    if remaining > 0:
        hours = remaining // 3600
        await update.message.reply_text(
            rf"{baidu_name_text}✅ 已授权，access\_token 剩余约 {hours} 小时有效期",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.message.reply_text(
            rf"{baidu_name_text}⚠️ access\_token 已过期，将在下次操作时自动刷新（refresh\_token 未过期）",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_reauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        return
    await db.delete_token(update.effective_user.id)
    await update.message.reply_text("✅ 已清除授权，请重新发送 /auth 进行授权")


# ─── 消息处理器 ─────────────────────────────────────────────────────────────────


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        return

    text: str = update.message.text or ""
    logger.info(f"收到用户 {update.effective_user.id} 的文本消息: {text}")

    # 1. 检查是否是百度 OAuth 回调 URL / code
    code = _extract_baidu_code(text)
    if code:
        logger.info(f"成功提取到百度 OAuth Code: {code}")
        await update.message.reply_text("🔄 正在验证授权码…")
        ok = await baidu_pan.exchange_code(update.effective_user.id, code)
        if ok:
            name_text = (
                f"（当前绑定百度账号：`{ok}`）"
                if isinstance(ok, str) and ok != "True"
                else ""
            )
            await update.message.reply_text(
                f"✅ 授权成功！{name_text}\n文件将转存到：`{config.BAIDU_SAVE_PATH}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                "❌ 授权失败，请检查链接是否正确，或重新 /auth"
            )
        return

    # 2. 检查是否含普通 URL
    urls = _extract_urls(text)
    if urls:
        for url in urls:
            await _handle_url(update, context, url)
        return

    await update.message.reply_text(
        "🤔 我不明白你发送的内容。\n"
        "• 给我发文件：直接发送文件\n"
        "• 给我下载链接：发送 URL\n"
        "• 首次使用：/auth 授权百度网盘"
    )


async def _handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id

    # 检查是否已授权
    token = await baidu_pan.get_valid_token(user_id)
    if not token:
        await update.message.reply_text("❌ 请先发送 /auth 完成百度网盘授权")
        return

    msg = await update.message.reply_text(
        f"⬇️ 准备下载链接…\n`{url[:100]}`", parse_mode=ParseMode.MARKDOWN_V2
    )
    notifier = ProgressNotifier(msg, f"⬇️ 正在下载链接…\n`{url[:100]}`")

    try:
        local_path = await download_url(url, user_id, progress_cb=notifier)
        filename = os.path.basename(local_path)
        await msg.delete()
        await _do_transfer(update, context, local_path, filename)
    except Exception as e:
        logger.exception(
            f"下载链接失败: {url}",
        )
        await msg.edit_text(f"❌ 下载失败：{str(e)[:200]}")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    token = await baidu_pan.get_valid_token(user_id)
    if not token:
        await update.message.reply_text("❌ 请先发送 /auth 完成百度网盘授权")
        return

    msg = update.message

    # 提取 file_id 和文件名
    if msg.document:
        file_id = msg.document.file_id
        filename = msg.document.file_name or f"document_{file_id[:8]}"
    elif msg.photo:
        photo = msg.photo[-1]
        file_id = photo.file_id
        filename = f"photo_{file_id[:8]}.jpg"
    elif msg.video:
        file_id = msg.video.file_id
        filename = msg.video.file_name or f"video_{file_id[:8]}.mp4"
    elif msg.audio:
        file_id = msg.audio.file_id
        filename = msg.audio.file_name or f"audio_{file_id[:8]}.mp3"
    elif msg.voice:
        file_id = msg.voice.file_id
        filename = f"voice_{file_id[:8]}.ogg"
    elif msg.animation:
        file_id = msg.animation.file_id
        filename = msg.animation.file_name or f"animation_{file_id[:8]}.mp4"
    elif msg.video_note:
        file_id = msg.video_note.file_id
        filename = f"video_note_{file_id[:8]}.mp4"
    elif msg.sticker:
        file_id = msg.sticker.file_id
        filename = f"sticker_{file_id[:8]}.webp"
    else:
        await msg.reply_text("⚠️ 不支持该文件类型")
        return

    # 校验文件大小（Telegram 官方 API 限制 bot 只能下载 20MB 以下的文件，Local Bot 限制 2GB）
    attachment = msg.effective_attachment
    if isinstance(attachment, list):
        attachment = attachment[-1]
    file_size = getattr(attachment, "file_size", 0)

    if file_size and file_size > 2000 * 1024 * 1024:
        mb_size = file_size / (1024 * 1024)
        await msg.reply_text(
            f"❌ 文件体积仍然过大 ({mb_size:.1f} MB)。\n\n"
            "即使使用 Local Bot API Server，Telegram 最大单文件限制亦不能超过 **2000 MB (2 GB)**。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not config.USE_LOCAL_API and file_size and file_size > 20 * 1024 * 1024:
        mb_size = file_size / (1024 * 1024)
        await msg.reply_text(
            f"❌ 文件体积过大 ({mb_size:.1f} MB)。\n\n"
            "受限于 Telegram 官方 Bot API，机器人直接接收文件最大不能超过 **20 MB**。\n"
            "想要突破此限制，请在环境变量中启用并配置 `USE_LOCAL_API=true` 及提供 `TG_API_ID` + `TG_API_HASH`。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    progress_msg = await msg.reply_text(
        f"⬇️ 准备从 Telegram 下载 `{filename}`…", parse_mode=ParseMode.MARKDOWN_V2
    )
    notifier = ProgressNotifier(progress_msg, f"⬇️ 正在从 Telegram 下载 `{filename}`")

    try:
        local_path = await download_telegram_file(
            context.bot, file_id, user_id, filename, progress_cb=notifier
        )
        await progress_msg.delete()
        await _do_transfer(update, context, local_path, filename)
    except Exception as e:
        logger.exception(f"下载 TG 文件失败: {file_id}")
        await progress_msg.edit_text(f"❌ 下载失败：{str(e)[:200]}")


# ─── 注册 handlers ───────────────────────────────────────────────────────────────


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reauth", cmd_reauth))

    # 文本消息
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # 文件类消息
    file_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
        | filters.VIDEO_NOTE
        | filters.Sticker.ALL
    )
    app.add_handler(MessageHandler(file_filter, handle_file))
