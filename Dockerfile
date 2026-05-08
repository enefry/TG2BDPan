FROM python:3.14-slim

# 安装 yt-dlp 需要的 ffmpeg，以及用于密码压缩的 zip
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl zip libmagic1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app

# 环境变量设置为不缓冲输出
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
RUN python3 /app/sensitive_detect.py update
CMD ["python", "/app/main.py"]
