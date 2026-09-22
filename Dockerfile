FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BATTERY_DB=/app/data/battery.db

WORKDIR /app

# 依赖安装（构建时完成）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码与静态资源
COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)" || exit 1

# 启动时自动完成 SQLite 初始化（见 app.main 的 startup 钩子）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
