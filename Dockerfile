# pushkey · 上游守护站
# ⚠️ Mac 是 aarch64，集群是 amd64 —— 构建必须显式指定平台，
#    否则镜像推上去是 exec format error。
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# 自测随镜像走：docker run --rm pushkey:vN python selftest.py
COPY selftest.py .

# 数据落盘目录（SQLite）。用非 root，且目录先建好给权限。
RUN mkdir -p /data && useradd -u 10001 -m pushkey && chown -R pushkey:pushkey /app /data
USER 10001

EXPOSE 8080

# 探针打这里
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
