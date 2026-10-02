.PHONY: help dev test lint bench check-history format migrate revision up down clean

help:
	@echo "Commandes disponibles pour BotRadar :"
	@echo "  make dev                 Lancer le serveur de développement FastAPI"
	@echo "  make test                Lancer les tests unitaires et d'intégration"
	@echo "  make bench               Benchmark du chemin d'alerte (invariant 500 ms)"
	@echo "  make lint                Vérifier le code avec Ruff et Mypy"
	@echo "  make check-history       Rejouer Ruff, Mypy et Pytest sur chaque commit"
	@echo "  make format              Formater le code avec Ruff"
	@echo "  make migrate             Appliquer les migrations Alembic"
	@echo "  make revision message=\"...\" Créer une nouvelle migration"
	@echo "  make up                  Démarrer les conteneurs Docker en arrière-plan"
	@echo "  make down                Arrêter les conteneurs Docker"
	@echo "  make clean               Nettoyer les fichiers de cache"

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest -v

bench:
	pytest tests/performance -v -s

lint:
	ruff check .
	mypy app

# Garantit qu'aucun commit de la branche n'est rouge : le travail est rejoué dans
# un worktree jetable par commit, du plus ancien au plus récent.
# Usage : make check-history | BASE=<tag|sha> | STEPS=ruff,mypy,pytest
check-history:
	python scripts/check_history.py $(if $(BASE),--base $(BASE),) \
		$(if $(STEPS),--steps $(STEPS),) $(if $(MAX_COMMITS),--max-commits $(MAX_COMMITS),)

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
