FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nightdesk ./nightdesk

RUN useradd --create-home --uid 1000 desk && mkdir -p /app/data && chown desk /app/data
USER desk

ENV DASHBOARD_HOST=0.0.0.0 DB_PATH=/app/data/nightdesk.sqlite3
EXPOSE 8080
CMD ["python", "-m", "nightdesk"]
