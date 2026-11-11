FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
VOLUME ["/data"]
EXPOSE 20100
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:20100/api/health')"
# 必须单进程运行：在线对话的连接表保存在进程内存里，不要加 --workers
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "20100", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws-max-size", "65536"]
