# Image de production — API FastAPI servie par uvicorn.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances d'abord (lock épinglé) pour profiter du cache de couches.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif + migrations
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Utilisateur non-root
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
