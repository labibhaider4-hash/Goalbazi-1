FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY goalbazi/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY goalbazi/ .

EXPOSE 8000

CMD ["sh", "-c", "python -m gunicorn server:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 2 --timeout 120 --log-level info --access-logfile - --error-logfile -"]
