# ==============================================================================
# Étape 1 : Builder
# ==============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python dans un virtualenv isolé
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Le code applicatif arrive plus bas par COPY : le wheel construit ici ne sert
# qu'à installer les dépendances. `requirements.lock` contraint chaque version,
# donc deux reconstructions de la même révision donnent le même environnement.
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -c requirements.lock .

# ==============================================================================
# Étape 2 : Runtime final
# ==============================================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Créer un utilisateur non privilégié pour exécuter l'application
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/sh -m appuser

RUN install -d -o appuser -g appgroup -m 0755 /var/lib/botradar

# Copier l'environnement virtuel depuis le builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copier le code source de l'application
COPY --chown=appuser:appgroup alembic.ini ./
COPY --chown=appuser:appgroup alembic/ ./alembic/
COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup scripts/ ./scripts/

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
