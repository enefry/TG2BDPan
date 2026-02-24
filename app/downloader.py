"""下载器模块

支持：
1. Telegram 文件下载（通过 Bot API 获取下载 URL，httpx 流式下载）
2. 普通 HTTP 直链下载（httpx 流式，支持重定向）
3. yt-dlp 下载（视频平台链接，B站、YouTube 等）
"""

import asyncio
import mimetypes
import os
import re
import urllib.parse
from typing import Optional, Callable, Awaitable

import aiofiles
import httpx
from loguru import logger
from telegram import Bot

from config import config

ProgressCallback = Callable[[int, int], Awaitable[None]]


def _ensure_tmp_dir(user_id: int) -> str:
    tmp: str = os.path.join(config.TMP_DIR, str(user_id))
    os.makedirs(tmp, exist_ok=True)
    return tmp


def _safe_filename(name: str) -> str:
    """去除文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:200] or "file"


async def download_telegram_file(
    bot: Bot,
    file_id: str,
    user_id: int,
    filename: Optional[str] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """
    下载 Telegram 文件到临时目录，返回本地路径。
    """
    tmp_dir: str = _ensure_tmp_dir(user_id)
    tg_file = await bot.get_file(file_id)
    file_url_or_path: str = tg_file.file_path or ""

    if filename:
        filename = _safe_filename(filename)
    else:
        filename = _safe_filename(os.path.basename(file_url_or_path))

    local_path: str = os.path.join(tmp_dir, filename)

    if (
        config.USE_LOCAL_API
        and os.path.isabs(file_url_or_path)
        and not file_url_or_path.startswith("http")
    ):
        # Local 模式：file_path 会是 /var/lib/telegram-bot-api 的本地路径，进行零下载拷贝
        import shutil

        shutil.copy2(file_url_or_path, local_path)
        logger.info(f"TG 文件(Local API)已复制到: {local_path}")
        if progress_cb and getattr(tg_file, "file_size", 0) > 0:
            await progress_cb(tg_file.file_size, tg_file.file_size)
    else:
        await _download_http(file_url_or_path, local_path, progress_cb)
        logger.info(
            f"TG 文件已下载: {local_path}",
        )
    return local_path


async def download_url(
    url: str,
    user_id: int,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """
    下载 URL 内容。
    - 如果是视频平台链接（YouTube、B站等），使用 yt-dlp。
    - 否则直接 HTTP 下载。
    返回本地文件路径。
    """
    tmp_dir: str = _ensure_tmp_dir(user_id)
    if _is_media_site(url):
        return await _download_ytdlp(url, tmp_dir, progress_cb)
    else:
        return await _download_direct(url, tmp_dir, progress_cb)


def _is_media_site(url: str) -> bool:
    """判断是否为视频/媒体平台链接"""
    patterns: list[str] = [
        r"youtube\.com",
        r"youtu\.be",
        r"bilibili\.com",
        r"b23\.tv",
        r"twitter\.com",
        r"x\.com",
        r"instagram\.com",
        r"tiktok\.com",
        r"v\.qq\.com",
        r"iqiyi\.com",
        r"youku\.com",
    ]
    return any(re.search(p, url, re.I) for p in patterns)


async def _download_http(
    url: str,
    local_path: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> None:
    """通用 httpx 流式下载"""
    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=300),
        follow_redirects=True,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total_size: int = int(resp.headers.get("content-length", 0))
            downloaded: int = 0
            async with aiofiles.open(local_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        await progress_cb(downloaded, total_size)


async def _download_direct(
    url: str,
    tmp_dir: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """下载普通 HTTP 直链，自动推断文件名"""
    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    cd: str = ""
    ct: str = "application/octet-stream"
    # 先 HEAD 获取文件名和类型
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers=headers,
    ) as client:
        try:
            head = await client.head(url)
            cd = head.headers.get("content-disposition", "")
            ct = head.headers.get("content-type", "application/octet-stream")
        except Exception:
            pass

    filename: str = _extract_filename_from_headers(cd, url, ct)
    local_path: str = os.path.join(tmp_dir, filename)

    await _download_http(url, local_path, progress_cb)
    logger.info(f"HTTP 文件已下载: local_path")
    return local_path


def _extract_filename_from_headers(
    content_disposition: str, url: str, content_type: str
) -> str:
    """从 Content-Disposition 或 URL 提取文件名"""
    # 尝试 Content-Disposition
    m = re.search(r'filename[^;=\n]*=[\'""]?([^\'""\n;]+)', content_disposition, re.I)
    if m:
        return _safe_filename(urllib.parse.unquote(m.group(1).strip(' "')))

    # 从 URL 路径提取
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path)
    if name and "." in name:
        return _safe_filename(urllib.parse.unquote(name))

    # 根据 Content-Type 推断扩展名
    ext: str = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    return f"download{ext}"


async def _download_ytdlp(
    url: str,
    tmp_dir: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """使用 yt-dlp 下载视频，返回下载的文件路径。注意 yt-dlp 暂不实现进度回调"""
    if progress_cb:
        await progress_cb(0, 0)  # 提示用户开始 yt-dlp 处理

    output_template: str = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    cmd: list[str] = [
        "yt-dlp",
        "--no-playlist",
        "--output",
        output_template,
        "--merge-output-format",
        "mp4",
        "--print",
        "after_move:filepath",  # 打印最终路径
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err: str = stderr.decode(errors="replace")
        raise RuntimeError(f"yt-dlp 下载失败:\n{err[-500:]}")

    if progress_cb:
        await progress_cb(1, 1)  # 提示用户下载完成

    # 从输出中提取最终文件路径
    output = stdout.decode(errors="replace").strip()
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if lines:
        filepath = lines[-1]
        if os.path.isfile(filepath):
            logger.info(f"yt-dlp 下载完成: filepath")
            return filepath

    # fallback：扫描 tmp_dir 找到最新文件
    files = sorted(
        [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)],
        key=os.path.getmtime,
        reverse=True,
    )
    if files:
        return files[0]

    raise RuntimeError("yt-dlp 下载完成但找不到输出文件")
