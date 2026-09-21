.PHONY: help dev worker test lint format migrate revision up down clean

help:
	@echo "Commandes disponibles pour Radar Bot :"
	@echo "  make dev                 Lancer le serveur de développement FastAPI"
	@echo "  make worker              Lancer le worker autonome de tâches de fond"
	@echo "  make test                Lancer les tests unitaires et d'intégration"
	@echo "  make lint                Vérifier le code avec Ruff et Mypy"
	@echo "  make format              Formater le code avec Ruff"
	@echo "  make migrate             Appliquer les migrations Alembic"
	@echo "  make revision message=\"...\" Créer une nouvelle migration"
	@echo "  make up                  Démarrer les conteneurs Docker en arrière-plan"
	@echo "  make down                Arrêter les conteneurs Docker"
	@echo "  make clean               Nettoyer les fichiers de cache"

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	@echo "Ne pas lancer en même temps que l'API (radar_app / make dev) : le scheduler serait doublé."
	python -m app.worker

test:
	pytest -v

lint:
	ruff check .
	mypy app

format:
	ruff format .
	ruff check --fix .

migrate:
	@if docker compose ps -q app 2>/dev/null | grep -q .; then \
		docker compose exec -e PYTHONPATH=/app app alembic upgrade head; \
	elif [ -f .venv/bin/alembic ]; then \
		.venv/bin/alembic upgrade head; \
	else \
		alembic upgrade head; \
	fi

revision:
	alembic revision --autogenerate -m "$(message)"

up:
	docker compose up -d

down:
	docker compose down

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
