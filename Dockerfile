FROM python:3.14-slim

# 安装 yt-dlp 需要的 ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app

# 环境变量设置为不缓冲输出
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "/app/main.py"]
