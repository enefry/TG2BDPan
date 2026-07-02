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
import shutil
import sys
import urllib.parse
from functools import lru_cache
from typing import Optional, Callable, Awaitable

import aiofiles
import httpx
from bs4 import BeautifulSoup
from loguru import logger
from PIL import Image
from telegram import Bot

from config import config

ProgressCallback = Callable[[int, int], Awaitable[None]]

_YTDLP_UPDATE_LOCK = asyncio.Lock()
_YTDLP_UPDATE_WARNING_RE = re.compile(
    r"yt-dlp version .* is older than 90 days|You installed yt-dlp with pip",
    re.I,
)
_HTML_ASSET_MIN_SIZE = 256
_HTML_ASSET_MAX_CANDIDATES = 200
_HTML_ASSET_CONCURRENCY = 5
_HTML_IMAGE_URL_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-original-src",
    "data-lazy-src",
    "data-url",
    "ess-data",
)
_REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
_FALLBACK_MEDIA_DOMAINS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "b23.tv",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tiktok.com",
    "douyin.com",
    "vimeo.com",
    "dailymotion.com",
    "facebook.com",
    "fb.watch",
    "reddit.com",
    "twitch.tv",
    "soundcloud.com",
    "bandcamp.com",
    "spotify.com",
    "nicovideo.jp",
    "niconico.jp",
    "pornhub.com",
    "xvideos.com",
    "xnxx.com",
    "v.qq.com",
    "iqiyi.com",
    "youku.com",
    "mgtv.com",
    "douban.com",
    "kuaishou.com",
    "weibo.com",
    "weibo.cn",
    "xiaohongshu.com",
    "ixigua.com",
    "sohu.com",
    "le.com",
    "163.com",
    "56.com",
)


def _ensure_tmp_dir(user_id: int) -> str:
    tmp: str = os.path.join(config.TMP_DIR, str(user_id))
    os.makedirs(tmp, exist_ok=True)
    return tmp


def _safe_filename(name: str) -> str:
    """去除文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|!]', "_", name)
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
    url = _replace_url_domain(url)
    tmp_dir: str = _ensure_tmp_dir(user_id)
    if _is_media_site(url):
        return await _download_ytdlp(url, tmp_dir, progress_cb)
    else:
        return await _download_direct(url, tmp_dir, progress_cb)


def _replace_url_domain(url: str) -> str:
    """按配置替换 URL 的域名和端口，保留路径、查询参数和 fragment"""
    if not config.URL_DOMAIN_REPLACEMENTS:
        return url

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return url

    replacement = config.URL_DOMAIN_REPLACEMENTS.get(parsed.netloc.lower())
    if not replacement:
        return url

    replaced = urllib.parse.urlunsplit(
        (parsed.scheme, replacement, parsed.path, parsed.query, parsed.fragment)
    )
    logger.info(f"URL 域名端口已替换: {url} -> {replaced}")
    return replaced


def _is_media_site(url: str) -> bool:
    """判断是否为 yt-dlp 支持的视频/媒体平台链接"""
    if _is_ytdlp_supported_url(url):
        return True

    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return False

    host = host.lower().strip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in _FALLBACK_MEDIA_DOMAINS
    )


def _is_ytdlp_supported_url(url: str) -> bool:
    """使用 yt-dlp 内置 extractor 判断 URL，避免手工维护 supported sites 列表"""
    try:
        return any(extractor.suitable(url) for extractor in _get_ytdlp_extractors())
    except Exception as exc:
        logger.debug(f"yt-dlp supported-site 判断失败，使用兜底域名列表: {exc}")
        return False


@lru_cache(maxsize=1)
def _get_ytdlp_extractors() -> tuple[type, ...]:
    from yt_dlp.extractor import gen_extractor_classes

    return tuple(
        extractor
        for extractor in gen_extractor_classes()
        if extractor.IE_NAME.lower() != "generic"
    )


async def _download_http(
    url: str,
    local_path: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> None:
    """通用 httpx 流式下载"""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=300),
        follow_redirects=True,
        headers=_REQUEST_HEADERS,
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
    cd: str = ""
    ct: str = "application/octet-stream"
    # 先 HEAD 获取文件名和类型
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers=_REQUEST_HEADERS,
    ) as client:
        try:
            head = await client.head(url)
            cd = head.headers.get("content-disposition", "")
            ct = head.headers.get("content-type", "application/octet-stream")
        except Exception:
            pass

    if _is_html_content_type(ct) or _is_probably_html_url(url):
        return await _download_html_assets(url, tmp_dir, progress_cb)

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


def _is_html_content_type(content_type: str) -> bool:
    return "text/html" in content_type.lower()


def _is_probably_html_url(url: str) -> bool:
    ext = _url_ext(url)
    return ext in {"", ".html", ".htm", ".shtml", ".xhtml"}


async def _download_html_assets(
    url: str,
    tmp_dir: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """下载 HTML 页面中的图片和 mp4，过滤低分辨率资源后返回资源目录"""
    if progress_cb:
        await progress_cb(0, 0)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=120),
        follow_redirects=True,
        headers=_REQUEST_HEADERS,
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not _is_html_content_type(content_type):
                filename = _extract_filename_from_headers(
                    resp.headers.get("content-disposition", ""),
                    str(resp.url),
                    content_type,
                )
                local_path = os.path.join(tmp_dir, filename)
                total_size = int(resp.headers.get("content-length", 0))
                downloaded = 0
                async with aiofiles.open(local_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            await progress_cb(downloaded, total_size)
                logger.info(f"HTTP 文件已下载: {local_path}")
                return local_path

            body = await resp.aread()
            page_url = str(resp.url)
            encoding = resp.encoding or "utf-8"

    soup = BeautifulSoup(body.decode(encoding, errors="replace"), "html.parser")
    candidates = _extract_html_asset_urls(soup, page_url)
    if not candidates:
        raise RuntimeError("网页中没有找到图片或 mp4 资源")

    page_name = _safe_filename(_html_page_name(soup, page_url))
    asset_dir = os.path.join(tmp_dir, page_name)
    if os.path.exists(asset_dir):
        shutil.rmtree(asset_dir, ignore_errors=True)
    os.makedirs(asset_dir, exist_ok=True)

    semaphore = asyncio.Semaphore(_HTML_ASSET_CONCURRENCY)
    accepted = 0
    failed = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=300),
        follow_redirects=True,
        headers=_REQUEST_HEADERS,
    ) as client:

        async def worker(index: int, asset_url: str) -> bool:
            async with semaphore:
                return await _download_one_html_asset(client, asset_url, asset_dir, index)

        tasks = [
            worker(index, asset_url)
            for index, asset_url in enumerate(
                candidates[:_HTML_ASSET_MAX_CANDIDATES], start=1
            )
        ]
        for done_count, task in enumerate(asyncio.as_completed(tasks), start=1):
            try:
                if await task:
                    accepted += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"网页资源下载失败: {e}")
            if progress_cb:
                await progress_cb(done_count, len(tasks))

    if accepted == 0:
        shutil.rmtree(asset_dir, ignore_errors=True)
        raise RuntimeError("网页资源下载完成，但没有符合尺寸要求的图片或 mp4")

    logger.info(f"网页资源下载完成: {asset_dir}, accepted={accepted}, skipped={failed}")
    return asset_dir


def _extract_html_asset_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    urls: list[str] = []

    def add(raw_url: str | None) -> None:
        if not raw_url:
            return
        raw_url = raw_url.strip()
        if not raw_url or raw_url.startswith(("data:", "blob:", "javascript:")):
            return
        absolute = urllib.parse.urljoin(page_url, raw_url)
        absolute, _ = urllib.parse.urldefrag(absolute)
        if absolute not in urls:
            urls.append(absolute)

    for img in soup.find_all("img"):
        for attr in _HTML_IMAGE_URL_ATTRS:
            add(img.get(attr))
        for src in _parse_srcset(str(img.get("srcset") or "")):
            add(src)

    for video in soup.find_all("video"):
        add(video.get("src"))
        add(video.get("poster"))

    for source in soup.find_all("source"):
        src = source.get("src")
        source_type = str(source.get("type") or "").lower()
        if src and ("video/mp4" in source_type or _url_ext(src) == ".mp4"):
            add(src)
        for srcset_url in _parse_srcset(str(source.get("srcset") or "")):
            add(srcset_url)

    for meta in soup.find_all("meta"):
        prop = str(meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if prop in {"og:image", "twitter:image", "og:video", "og:video:url"}:
            add(content)

    return urls


def _parse_srcset(srcset: str) -> list[str]:
    urls: list[str] = []
    for part in srcset.split(","):
        candidate = part.strip().split()
        if candidate:
            urls.append(candidate[0])
    return urls


def _url_ext(url: str) -> str:
    return os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()


def _html_page_name(soup: BeautifulSoup, page_url: str) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    parsed = urllib.parse.urlparse(page_url)
    name = os.path.basename(parsed.path.rstrip("/"))
    return name or parsed.netloc or "webpage_assets"


async def _download_one_html_asset(
    client: httpx.AsyncClient,
    asset_url: str,
    asset_dir: str,
    index: int,
) -> bool:
    async with client.stream("GET", asset_url) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        ext = _asset_extension(asset_url, content_type)
        if not ext:
            return False

        filename = _safe_filename(f"{index:04d}_{_asset_basename(asset_url, ext)}")
        local_path = os.path.join(asset_dir, filename)
        async with aiofiles.open(local_path, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                await f.write(chunk)

    if await _is_low_resolution_asset(local_path, ext):
        os.remove(local_path)
        return False

    logger.info(f"网页资源已下载: {local_path}")
    return True


def _asset_extension(asset_url: str, content_type: str) -> str:
    ext = _url_ext(asset_url)
    if ext == ".mp4":
        return ".mp4"
    if content_type == "video/mp4":
        return ".mp4"
    if content_type.startswith("image/"):
        return mimetypes.guess_extension(content_type) or ext or ".img"
    if ext and (mimetypes.guess_type(f"file{ext}")[0] or "").startswith("image/"):
        return ext
    return ""


def _asset_basename(asset_url: str, ext: str) -> str:
    name = os.path.basename(urllib.parse.urlparse(asset_url).path)
    if not name:
        return f"asset{ext}"
    base, current_ext = os.path.splitext(name)
    if current_ext:
        return name
    return f"{base}{ext}"


async def _is_low_resolution_asset(local_path: str, ext: str) -> bool:
    if ext == ".mp4":
        size = await _probe_video_size(local_path)
        if size is None:
            return False
        width, height = size
        return width < _HTML_ASSET_MIN_SIZE or height < _HTML_ASSET_MIN_SIZE

    try:
        with Image.open(local_path) as image:
            width, height = image.size
        return width < _HTML_ASSET_MIN_SIZE or height < _HTML_ASSET_MIN_SIZE
    except Exception as e:
        logger.warning(f"图片尺寸读取失败，跳过资源: {local_path}, error={e}")
        return True


async def _probe_video_size(local_path: str) -> tuple[int, int] | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        local_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            f"mp4 分辨率读取失败，保留资源: {local_path}, error={stderr.decode(errors='replace')[-300:]}"
        )
        return None

    text = stdout.decode(errors="replace").strip()
    match = re.search(r"(\d+)x(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


async def _download_ytdlp(
    url: str,
    tmp_dir: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """使用 yt-dlp 下载视频，返回下载的文件路径。注意 yt-dlp 暂不实现进度回调"""
    if progress_cb:
        await progress_cb(0, 0)  # 提示用户开始 yt-dlp 处理

    output_template: str = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    cmd = _build_ytdlp_cmd(output_template, url)
    returncode, stdout, stderr = await _run_ytdlp(cmd)

    if returncode != 0 and _YTDLP_UPDATE_WARNING_RE.search(stderr):
        logger.warning("yt-dlp 版本过旧，正在自动更新后重试下载")
        if await _update_ytdlp():
            returncode, stdout, stderr = await _run_ytdlp(cmd)
        else:
            raise RuntimeError(f"yt-dlp 自动更新失败，原始错误:\n{stderr[-500:]}")

    if returncode != 0 and _can_retry_ytdlp_with_cookies(cmd):
        logger.warning("yt-dlp 无 cookies 下载失败，正在使用 cookies 重试")
        retry_cmd = _build_ytdlp_cmd(output_template, url, with_cookies=True)
        retry_code, retry_stdout, retry_stderr = await _run_ytdlp(retry_cmd)
        if retry_code == 0:
            returncode, stdout, stderr = retry_code, retry_stdout, retry_stderr
        else:
            logger.warning(f"yt-dlp cookies 重试失败: {retry_stderr[-500:]}")
            returncode, stdout, stderr = retry_code, retry_stdout, retry_stderr

    if returncode != 0:
        raise RuntimeError(f"yt-dlp 下载失败:\n{stderr[-500:]}")

    if stderr and _YTDLP_UPDATE_WARNING_RE.search(stderr):
        logger.warning("yt-dlp 版本过旧，下载成功后自动更新")
        await _update_ytdlp()

    if progress_cb:
        await progress_cb(1, 1)  # 提示用户下载完成

    return _find_ytdlp_output_file(stdout, tmp_dir)


def _build_ytdlp_cmd(
    output_template: str, url: str, with_cookies: bool = False
) -> list[str]:
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
    if config.YTDLP_FORMAT:
        cmd[1:1] = ["--format", config.YTDLP_FORMAT]
    if config.YTDLP_FORMAT_SORT:
        cmd[1:1] = [
            "--format-sort-force",
            "--format-sort",
            config.YTDLP_FORMAT_SORT,
        ]
    if with_cookies and config.YTDLP_COOKIES_FILE:
        if os.path.isfile(config.YTDLP_COOKIES_FILE):
            cmd[1:1] = ["--cookies", config.YTDLP_COOKIES_FILE]
        else:
            logger.warning(f"yt-dlp cookies 文件不存在: {config.YTDLP_COOKIES_FILE}")
    elif with_cookies and config.YTDLP_COOKIES_FROM_BROWSER:
        cmd[1:1] = ["--cookies-from-browser", config.YTDLP_COOKIES_FROM_BROWSER]
    if with_cookies and _is_youtube_url(url) and config.YTDLP_YOUTUBE_PLAYER_CLIENT:
        cmd[1:1] = [
            "--extractor-args",
            f"youtube:player_client={config.YTDLP_YOUTUBE_PLAYER_CLIENT}",
        ]
    return cmd


def _is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.I))


def _can_retry_ytdlp_with_cookies(cmd: list[str]) -> bool:
    has_cookie_source = (
        bool(config.YTDLP_COOKIES_FILE)
        and os.path.isfile(config.YTDLP_COOKIES_FILE)
    ) or bool(config.YTDLP_COOKIES_FROM_BROWSER)
    return (
        has_cookie_source
        and "--cookies" not in cmd
        and "--cookies-from-browser" not in cmd
    )


async def _run_ytdlp(cmd: list[str]) -> tuple[int, str, str]:
    """运行 yt-dlp，失败时返回 stderr 给调用方判断是否需要自动更新"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")

    return proc.returncode, out, err


async def _update_ytdlp() -> bool:
    """通过当前 Python 环境的 pip 更新 yt-dlp，避免 Docker 内二进制路径不一致"""
    async with _YTDLP_UPDATE_LOCK:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--upgrade",
            "yt-dlp[default]",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        if proc.returncode == 0:
            logger.info("yt-dlp 自动更新完成")
            return True

        logger.error(f"yt-dlp 自动更新失败: {(err or out)[-1000:]}")
        return False


def _find_ytdlp_output_file(output: str, tmp_dir: str) -> str:
    """根据 yt-dlp 输出或临时目录扫描定位最终文件"""
    # 从输出中提取最终文件路径
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if lines:
        filepath = lines[-1]
        if os.path.isfile(filepath):
            logger.info(f"yt-dlp 下载完成: {filepath}")
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
