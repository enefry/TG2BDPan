# Telegram Bot 转存百度网盘服务

这是一个用于接收 Telegram 用户发送的文件、链接（含视频平台链接），并自动下载并转存到百度网盘的后台服务。

## 功能特性

- 支持直接转发 telegram 上的文件（文档、图片、视频、音频）
- 支持提取消息文本中的纯 HTTP 链接并下载
- 支持 `yt-dlp` 解析的视频链接（如 BiliBili，YouTube 等）下载
- 支持按配置自动替换下载链接的域名和端口
- OAuth 绑定百度网盘，采用 `sqlite3` 本地持久化凭证，过期自动刷新
- 全异步实现，使用 `python-telegram-bot` v21+ 和 `httpx`，支持大文件分片并行。

## 前置准备

1. 去 [@BotFather](https://t.me/BotFather) 申请一个 Telegram Bot Token。
2. 去 [百度开放平台](https://pan.baidu.com/union/home) 申请一个应用，获取 **AppKey** 和 **SecretKey**。
   - **重要配置**：在开放平台应用的“安全设置”中，需要把 OAuth 授权回调页填写为 `oob`（允许页面显示授权码），或者你在 `.env` 配置自定义回调。

## 部署运行

项目使用 Docker Compose 进行部署：

```bash
git clone <repository_url> pan_saver
cd pan_saver

# 1. 复制配置并填写
cp .env.example .env

# 编辑 .env 文件，填入 Token 和 Key
nano .env

# 2. 启动服务
docker compose up -d

# 3. 查看运行日志
docker compose logs -f
```

## 使用说明

1. 在 Telegram 找到你的 Bot，发送 `/start`。
2. 发送 `/auth` 获取百度网盘授权链接。
3. 点击链接授权后，页面会跳转。如果你没有配置真实回调地址（`BAIDU_REDIRECT_URI=oob`），页面可能会显示一段 JSON 或空白，**请直接复制浏览器地址栏中的完整 URL**。
4. 将复制的 URL（例如 `https://openapi.baidu.com/oauth/2.0/login_success#code=xxxxx...`）或者直接提取 `code=xxxx` 后的代码，发送给 Bot。
5. Bot 提示授权成功后，即可：
   - 给 Bot 发送任意文件
   - 给 Bot 发送包含链接的文本
     Bot 会自动下载并转存到你的网盘中。

## 登录与切换账号

这个 Bot 不需要登录 Telegram 账号；它使用你正在聊天的 Telegram 用户 ID 作为身份。每个 Telegram 用户 ID 在本地数据库中独立绑定一个百度网盘账号。

常用命令：

```text
/auth    获取百度网盘 OAuth 授权链接
/status  查看当前 Telegram 用户绑定的百度网盘账号和 token 状态
/reauth  清除当前绑定，重新授权
```

首次绑定百度网盘：

1. 在 Telegram 给 Bot 发送 `/auth`。
2. 用浏览器打开 Bot 返回的授权链接。
3. 在浏览器里登录并确认授权目标百度账号。
4. 授权后复制浏览器地址栏里的完整 URL，或者复制其中的 `code=...` 授权码。
5. 把 URL 或授权码发回 Bot。
6. Bot 回复“授权成功”后，后续文件会转存到这个百度账号。

切换百度网盘账号：

1. 在 Telegram 给 Bot 发送 `/reauth`，清除当前 Telegram 用户绑定的百度 token。
2. 在浏览器里退出当前百度账号，或切换到你想绑定的新百度账号。
3. 再给 Bot 发送 `/auth`，打开新的授权链接。
4. 用目标百度账号确认授权。
5. 将跳转后的完整 URL 或 `code=...` 发回 Bot。
6. 发送 `/status` 确认“当前绑定账号”已经变成目标账号。

如果 `/auth` 打开后总是授权到旧百度账号，问题通常在浏览器端：百度登录态还停留在旧账号。请先在浏览器退出百度账号，或用无痕窗口/另一个浏览器打开授权链接。

## URL 域名端口替换

如果需要把传入链接中的指定 `域名:端口` 替换为另一个 `域名:端口`，推荐在 `data/config.json` 配置。可以参考 `data/config.sample.json`：

```json
{
  "url_domain_replacements": {
    "old.example.com:8080": "new.example.com:9090",
    "10.0.0.2:8000": "public.example.com:443"
  }
}
```

也可以继续在 `.env` 配置：

```dotenv
URL_DOMAIN_REPLACEMENTS=old.example.com:8080=new.example.com:9090,10.0.0.2:8000=public.example.com:443
```

例如收到 `http://old.example.com:8080/path/file.mp4?token=abc` 时，会下载 `http://new.example.com:9090/path/file.mp4?token=abc`。替换只改变域名和端口，路径、查询参数和 fragment 会保留。

默认 JSON 配置路径是 `data/config.json`；Docker 内路径是 `/app/data/config.json`。如果需要改路径，可以设置：

```dotenv
CONFIG_FILE=/app/data/config.json
```

## Twitter/X 视频下载

Twitter/X 链接会自动走 `yt-dlp` 下载公开视频。默认先不使用 cookies；只有无 cookies 下载失败，且配置了 cookies 文件时，才会用 cookies 重试一次。

如果遇到需要登录态、年龄限制或反爬校验的视频，可以导出 Netscape 格式 cookies 到 `data/ytdlp-cookies.txt`，然后在 `data/config.json` 配置：

```json
{
  "ytdlp_cookies_file": "/app/data/ytdlp-cookies.txt"
}
```

也可以在 `.env` 配置：

```dotenv
YTDLP_COOKIES_FILE=/app/data/ytdlp-cookies.txt
```

也可以直接读取已挂载的 Firefox profile。先把宿主机 profile 目录挂载到 `pan_saver` 容器，例如挂载到 `/data/mozilla`，再配置：

```json
{
  "ytdlp_cookies_from_browser": "firefox:/data/mozilla/firefox/7u8bnsv1.default-esr"
}
```

或者在 `.env` 配置：

```dotenv
YTDLP_COOKIES_FROM_BROWSER=firefox:/data/mozilla/firefox/7u8bnsv1.default-esr
```

YouTube 在部分服务器 IP 上即使带 cookies 也可能在下载媒体流时返回 403。服务默认在 YouTube cookies 重试时使用 `mweb` player client 提高成功率；如需调整，可以配置：

```json
{
  "ytdlp_youtube_player_client": "mweb"
}
```

或在 `.env` 配置：

```dotenv
YTDLP_YOUTUBE_PLAYER_CLIENT=mweb
```

默认会优先选择 mp4/m4a 格式，并按 HEVC、H.264、AV1 的顺序偏好视频编码。可以按需覆盖：

```json
{
  "ytdlp_format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
  "ytdlp_format_sort": "vcodec:hevc:h264:av1,res,fps,br"
}
```

或在 `.env` 配置：

```dotenv
YTDLP_FORMAT=bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b
YTDLP_FORMAT_SORT=vcodec:hevc:h264:av1,res,fps,br
```

## 自定义敏感词替换

文件名上传前会先执行自定义替换，再走内置敏感词库替换。推荐在 `data/config.json` 中配置：

```json
{
  "custom_sensitive_words": {
    "王局拍案": "wj"
    }
}
```

服务会监控 `CONFIG_FILE` 指向的 JSON 文件，文件变化后自动刷新运行期配置；修改 `custom_sensitive_words`、`url_domain_replacements`、`yt-dlp` 相关配置后无需重启。
