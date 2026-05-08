from pathlib import Path
from typing import Dict, Any
from io import BytesIO
from os import PathLike
from PIL import Image
import httpx
from loguru import logger
import magic
import tempfile
import os
import asyncio


async def file_detect(file: bytes | str | PathLike) -> str:
    return magic.from_file(file, mime=True)


async def _run_cmd(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    logger.info(f'Running command: {" ".join(args)}')
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    return (
         proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="ignore"),
        stderr.decode("utf-8", errors="ignore"),
    )


async def _get_video_duration(file_path: str) -> float | None:
    code, out, _ = await _run_cmd(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
        timeout=15,
    )

    if code != 0:
        return None

    try:
        duration = float(out.strip())
        return duration if duration > 0 else None
    except ValueError:
        return None


async def _extract_video_frames(
    file_path: str,
    output_dir: str,
    count: int = 3,
) -> list[str]:
    duration = await _get_video_duration(file_path)

    if duration:
        # 取 20%、50%、80% 三个点，避开纯黑片头片尾
        ratios = [0.2, 0.5, 0.8]
        timestamps = [max(0.0, duration * r) for r in ratios[:count]]
    else:
        # 拿不到时长时兜底
        timestamps = [0.0, 3.0, 8.0][:count]

    frames: list[str] = []

    for index, ts in enumerate(timestamps):
        output_path = str(Path(output_dir) / f"frame{index}.jpg")

        code, _, err = await _run_cmd(
            "ffmpeg",
            "-y",
            "-ss",
            f"{ts:.3f}",
            "-i",
            file_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-update",  # 这里加上
            "1",
            output_path,
            timeout=30,
        )
        logger.info(f"[nsfw] Extracted frame: {output_path}, timestamp: {ts:.3f}, returncode: {code}, stderr: {err}")
        if (
            code == 0
            and Path(output_path).exists()
            and Path(output_path).stat().st_size > 0
        ):
            frames.append(output_path)
        else:
            logger.error(f"[nsfw] extract frame failed ts={ts:.3f}: {err}")

    return frames


def _is_nsfw_result(result: dict[str, Any]) -> bool:
    label = str(result.get("label", "")).lower()
    return bool(label and label != "normal")


async def nsfw_detect(file_path: str) -> bool:
    if os.path.isdir(file_path):
        return False
    mime_type = await file_detect(file_path)
    mime_type = (mime_type or "").lower()

    if "image" in mime_type:
        nsfw_result = await _nsfw_detect(file_path)
        return _is_nsfw_result(nsfw_result)

    if "video" in mime_type:
        with tempfile.TemporaryDirectory(prefix="nsfw_frames_") as tmpdir:
            frames = await _extract_video_frames(file_path, tmpdir, count=3)
            logger.info(f'Extracted video frames: {frames}')
            for frame_path in frames:
                try:
                    nsfw_result = await _nsfw_detect(frame_path)
                    if _is_nsfw_result(nsfw_result):
                        return True
                except Exception as e:
                    logger.error(f"[nsfw] detect frame failed: {frame_path}, error={e}")

        return False

    return False


async def _nsfw_detect(
    image_path: str,
    api_key: str = "hf_a49d561c-72ce-5e05-8d5b-b7c92834de62",
    url: str = "https://james4096-iopaint.hf.space/nsfw",
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """
    上传图片到 NSFW 检测接口

    Args:
        image_path: 图片路径
        api_key: X-API-Key
        url: 服务地址
        timeout: 请求超时时间

    Returns:
        JSON 响应
        {
            "label": "hentai",
            "score": 0.999927,
            "scores": {
                "safe": 0,
                "hentai": 0.999927,
                "porn": 0.000073,
                "sexy": 0,
                "drawing": 0
            },
            "model": "viddexa/nsfw-detection-2-mini"
        }
        # Normal: Photos without nsfw content.不含 NSFW 内容的照片。
        # Porn: Photos with pornographic content.包含色情内容的照片。
        # Hentai: Drawing with sexual content.包含性内容的绘画作品。
        # Drawing: Comics, cartoons or drawings with no nsfw content.不含 NSFW 内容的漫画、卡通或插画。
        # Sexy: Photos with no explicit nudity but might contain some characteristics of nsfw content such as suggestive nudity or risqué clothing. 不含露骨裸露内容，但可能带有某些 NSFW 特征（如暗示性裸露或衣着暴露）的照片。
    """
    headers = {
        "accept": "application/json",
        "X-API-Key": api_key,
    }

    image_file = Path(image_path)

    if not image_file.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    suffix = image_file.suffix.lower()

    # PNG 直接上传
    if suffix == ".png":
        with image_file.open("rb") as f:
            files = {
                "image": (
                    image_file.name,
                    f,
                    "image/png",
                )
            }

            response = httpx.post(
                url,
                headers=headers,
                files=files,
                timeout=timeout,
            )

    else:
        # 非 PNG 自动转 PNG
        image = Image.open(image_file)

        # 避免 RGBA/P 等格式问题
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        png_buffer = BytesIO()
        image.save(png_buffer, format="PNG")
        png_buffer.seek(0)

        files = {
            "image": (
                f"{image_file.stem}.png",
                png_buffer,
                "image/png",
            )
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                files=files,
                timeout=timeout,
            )

    response.raise_for_status()
    result = response.json()
    logger.info(f"NSFW Detection: path={image_path}  Result= {result}")
    return result


if __name__ == "__main__":
    import asyncio

    asyncio.run(nsfw_detect("/media/data/service/pan_saver/app/test2.png"))
