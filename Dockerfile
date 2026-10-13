FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    LB_ALLOW_LOCAL=0
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py unshorten.py ./
COPY templates ./templates
COPY static ./static
EXPOSE 10000
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 240 --graceful-timeout 30 app:app"]
