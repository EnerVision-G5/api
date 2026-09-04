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

# L'entrée applique les migrations avant de passer la main a la commande.
# chmod explicite : le bit d'exécution ne survit pas à un checkout Windows.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Utilisateur non-root
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
