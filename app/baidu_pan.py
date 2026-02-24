"""百度网盘 OpenAPI 封装（全异步，使用 httpx）"""

import asyncio
import hashlib
import os
import time
import urllib.parse
from typing import Optional, Any, Callable, Awaitable

import httpx
from loguru import logger

import db
from config import config

OAUTH_BASE: str = "https://openapi.baidu.com/oauth/2.0"
PCS_BASE: str = "https://pan.baidu.com/rest/2.0/xpan"
UPLOAD_BASE: str = "https://d.pcs.baidu.com/rest/2.0/pcs"

CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB

ProgressCallback = Callable[[int, int], Awaitable[None]]


def get_auth_url() -> str:
    """生成百度网盘 OAuth 授权 URL"""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": config.BAIDU_APP_KEY,
        "redirect_uri": config.BAIDU_REDIRECT_URI,
        "scope": "basic,netdisk",
        "display": "page",
    }
    return f"{OAUTH_BASE}/authorize?" + urllib.parse.urlencode(params)


async def exchange_code(user_id: int, code: str) -> bool | str:
    """用授权码换取 token 并保存到数据库。成功返回 True 或百度用户名，失败返回 False"""
    params: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.BAIDU_APP_KEY,
        "client_secret": config.BAIDU_SECRET_KEY,
        "redirect_uri": config.BAIDU_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{OAUTH_BASE}/token", params=params)
        data = resp.json()

    if "access_token" not in data:
        logger.error(f"exchange_code 失败: {data}")
        return False

    # 尝试获取绑定的百度网盘用户名
    uinfo = await get_uinfo(user_id, data["access_token"])
    baidu_name = uinfo.get("baidu_name") if uinfo else None

    await db.save_token(
        user_id=user_id,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 2592000),
        baidu_name=baidu_name,
    )

    if baidu_name:
        logger.info(f"成功绑定百度网盘账号: {baidu_name}")
        return baidu_name

    return True


async def get_uinfo(
    user_id: int, access_token: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """获取百度网盘用户信息（头像、用户名、VIP等级等）"""
    if not access_token:
        access_token = await get_valid_token(user_id)
    if not access_token:
        return None

    params: dict[str, str] = {"method": "uinfo", "access_token": access_token}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{PCS_BASE}/nas", params=params)
            data = resp.json()
            if data.get("errno", -1) == 0:
                return data
            logger.error(f"获取百度用户信息失败: {data}")
        except Exception as e:
            logger.exception("获取百度用户信息请求出错")

    return None


async def _refresh_token(user_id: int, refresh_token: str) -> Optional[str]:
    """刷新 access_token，更新数据库，返回新的 access_token 或 None"""
    params: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.BAIDU_APP_KEY,
        "client_secret": config.BAIDU_SECRET_KEY,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{OAUTH_BASE}/token", params=params)
        data = resp.json()

    if "access_token" not in data:
        logger.error(f"刷新 token 失败: {data}")
        return None

    await db.save_token(
        user_id=user_id,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", refresh_token),
        expires_in=data.get("expires_in", 2592000),
    )
    return data["access_token"]


async def get_valid_token(user_id: int) -> Optional[str]:
    """获取有效的 access_token，自动刷新。未授权则返回 None"""
    record = await db.get_token(user_id)
    if not record:
        return None

    # 提前 1 天刷新以便配合后台定时任务
    if time.time() >= record["expires_at"] - 86400:
        logger.info(f"为用户 {user_id} 自动续期 Access Token")
        token = await _refresh_token(user_id, record["refresh_token"])
        return token

    return record["access_token"]


def _md5_of_file_chunk(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


async def upload_file(
    user_id: int,
    local_path: str,
    remote_filename: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """
    将本地文件上传至百度网盘。
    返回 {"ok": True, "path": ...} 或 {"ok": False, "error": ...}
    """
    access_token = await get_valid_token(user_id)
    if not access_token:
        return {"ok": False, "error": "未授权，请先发送 /auth 完成授权"}

    remote_path: str = f"{config.BAIDU_SAVE_PATH.rstrip('/')}/{remote_filename}"
    file_size: int = os.path.getsize(local_path)
    block_list: list[str] = []
    chunks: list[bytes] = []

    # 读取分块并计算 MD5
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            block_list.append(_md5_of_file_chunk(chunk))
            chunks.append(chunk)

    if progress_cb:
        await progress_cb(0, file_size)

    # 1. 预创建
    precreate_data: dict[str, Any] = {
        "path": remote_path,
        "size": file_size,
        "isdir": 0,
        "autoinit": 1,
        "block_list": str(block_list).replace("'", '"'),
        "rtype": 1,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{PCS_BASE}/file",
            params={"method": "precreate", "access_token": access_token},
            data=precreate_data,
        )
        pre = resp.json()

    if "uploadid" not in pre:
        logger.error(f"precreate 失败: {pre}")
        return {"ok": False, "error": f"预创建失败: {pre.get('errmsg', pre)}"}

    upload_id: str = pre["uploadid"]
    uploaded_size: int = 0

    # 2. 分片上传
    async with httpx.AsyncClient(timeout=300) as client:
        for idx, chunk in enumerate(chunks):
            chunk_len = len(chunk)
            for attempt in range(3):
                try:
                    resp = await client.post(
                        f"{UPLOAD_BASE}/superfile2",
                        params={
                            "method": "upload",
                            "access_token": access_token,
                            "type": "tmpfile",
                            "path": remote_path,
                            "uploadid": upload_id,
                            "partseq": str(idx),
                        },
                        files={
                            "file": (remote_filename, chunk, "application/octet-stream")
                        },
                    )
                    result = resp.json()
                    if "md5" in result:
                        break
                except Exception as e:
                    if attempt == 2:
                        return {"ok": False, "error": f"分片 {idx} 上传失败: {e}"}
                    await asyncio.sleep(2)

            uploaded_size += chunk_len
            if progress_cb:
                await progress_cb(uploaded_size, file_size)

    # 3. 合并创建
    create_data: dict[str, Any] = {
        "path": remote_path,
        "size": file_size,
        "isdir": 0,
        "uploadid": upload_id,
        "block_list": str(block_list).replace("'", '"'),
        "rtype": 1,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{PCS_BASE}/file",
            params={"method": "create", "access_token": access_token},
            data=create_data,
        )
        result = resp.json()

    if result.get("errno", -1) != 0:
        logger.error(f"create 失败: {result}")
        return {"ok": False, "error": f"创建文件失败: {result.get('errmsg', result)}"}

    return {"ok": True, "path": remote_path}
