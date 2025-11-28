FROM python:3.9-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# 默认5分钟抓一次
ENV FETCH_INTERVAL=300

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 暴露 8080 端口给 Zeabur
EXPOSE 8080

CMD ["python", "main.py"]
