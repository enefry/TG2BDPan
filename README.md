# Telegram Bot 转存百度网盘服务

这是一个用于接收 Telegram 用户发送的文件、链接（含视频平台链接），并自动下载并转存到百度网盘的后台服务。

## 功能特性

- 支持直接转发 telegram 上的文件（文档、图片、视频、音频）
- 支持提取消息文本中的纯 HTTP 链接并下载
- 支持 `yt-dlp` 解析的视频链接（如 BiliBili，YouTube 等）下载
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
