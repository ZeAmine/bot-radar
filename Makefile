.PHONY: help dev test lint bench check-history format migrate revision up down clean \
        lock check-lock preflight verify-deploy deploy push

# Les outils viennent du venv du dépôt quand il existe, sinon du PATH. `make` doit
# fonctionner sans `source .venv/bin/activate` : en local le venv est là, en CI il
# n'y est pas mais les outils sont installés. Un appel explicite reste prioritaire :
# `make PYTHON=/usr/bin/python3.12 test`.
#
# Le repli est le nom nu du binaire, pas une chaîne vide : si l'outil n'est nulle
# part, la recette échoue sur « command not found », ce qui se lit. Une résolution
# vide exécuterait à la place un début de recette tronqué (`make test` deviendrait
# `-v`), c'est-à-dire une erreur incompréhensible.
PYTHON  ?= $(shell test -x .venv/bin/python  && echo .venv/bin/python  || echo python3)
RUFF    ?= $(shell test -x .venv/bin/ruff    && echo .venv/bin/ruff    || echo ruff)
MYPY    ?= $(shell test -x .venv/bin/mypy    && echo .venv/bin/mypy    || echo mypy)
PYTEST  ?= $(shell test -x .venv/bin/pytest  && echo .venv/bin/pytest  || echo pytest)
ALEMBIC ?= $(shell test -x .venv/bin/alembic && echo .venv/bin/alembic || echo alembic)
UVICORN ?= $(shell test -x .venv/bin/uvicorn && echo .venv/bin/uvicorn || echo uvicorn)

help:
	@echo "Commandes disponibles pour BotRadar :"
	@echo "  make dev                 Lancer le serveur de développement FastAPI"
	@echo "  make test                Lancer les tests unitaires et d'intégration"
	@echo "  make bench               Benchmark du chemin d'alerte (invariant 500 ms)"
	@echo "  make lint                Vérifier le code avec Ruff et Mypy"
	@echo "  make lock                Régénérer requirements.lock depuis l'environnement courant"
	@echo "  make check-lock          Vérifier que le verrou couvre pyproject.toml"
	@echo "  make check-history       Rejouer Ruff, Mypy et Pytest sur chaque commit"
	@echo "  make preflight           Vérifier le VPS avant une synchronisation"
	@echo "  make verify-deploy       Revérifier le VPS après une synchronisation"
	@echo "  make deploy              Déployer enchaîné (pré-vol → synchro → build → migration → contrôle)"
	@echo "  make push                Pousser sur main en levant la protection, puis la restaurant"
	@echo "  make format              Formater le code avec Ruff"
	@echo "  make migrate             Appliquer les migrations Alembic"
	@echo "  make revision message=\"...\" Créer une nouvelle migration"
	@echo "  make up                  Démarrer les conteneurs Docker en arrière-plan"
	@echo "  make down                Arrêter les conteneurs Docker"
	@echo "  make clean               Nettoyer les fichiers de cache"

dev:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(PYTEST) -v

bench:
	$(PYTEST) tests/performance -v -s

lint:
	$(RUFF) check .
	$(MYPY) app

# Le verrouillage n'a de valeur que s'il suit `pyproject.toml`. Ce contrôle est
# hors réseau : il échoue si une dépendance a été ajoutée sans être épinglée.
check-lock:
	$(PYTHON) scripts/check_lock.py

# Régénération à faire après toute modification de `pyproject.toml`, puis commiter
# le fichier : c'est lui que le Dockerfile et la CI lisent. Les commentaires
# d'en-tête sont conservés, seule la liste des versions est réécrite.
lock:
	@grep "^#" requirements.lock > requirements.lock.new
	@$(PYTHON) -m pip freeze \
		| grep -v "^-e " \
		| grep -viE "^(botradar|radar_bot|radar-bot)==" \
		| grep -E "^[A-Za-z0-9._-]+==" \
		| sort -f >> requirements.lock.new
	@mv requirements.lock.new requirements.lock
	@$(MAKE) --no-print-directory check-lock

# Garantit qu'aucun commit de la branche n'est rouge : le travail est rejoué dans
# un worktree jetable par commit, du plus ancien au plus récent.
# Usage : make check-history | BASE=<tag|sha> | STEPS=ruff,mypy,pytest
check-history:
	$(PYTHON) scripts/check_history.py $(if $(BASE),--base $(BASE),) \
		$(if $(STEPS),--steps $(STEPS),) $(if $(MAX_COMMITS),--max-commits $(MAX_COMMITS),)

# Un push ne déploie rien : le VPS est une cible rsync, pas un clone. Ces deux
# commandes encadrent la synchronisation pour que l'écart soit visible.
preflight:
	$(PYTHON) scripts/preflight_deploy.py

verify-deploy:
	$(PYTHON) scripts/preflight_deploy.py --post-sync

# La procédure complète, dans l'ordre, en s'arrêtant au premier échec. Les étapes
# individuelles restent disponibles : un déploiement interrompu se reprend à la main.
# Usage : make deploy | DEPLOY_ARGS=--dry-run
deploy:
	$(PYTHON) scripts/deploy.py $(if $(DEPLOY_ARGS),$(DEPLOY_ARGS),)

# La CI ne peut pas tourner sur un commit qui n'existe pas encore sur GitHub : le
# status check obligatoire bloque donc le push direct. La cible lève la protection,
# pousse, et la remet quoi qu'il arrive (restauration dans un `finally`).
# Usage : make push | PUSH_ARGS=--dry-run
push:
	$(PYTHON) scripts/push_protected.py $(if $(PUSH_ARGS),$(PUSH_ARGS),)

format:
	$(RUFF) format .
	$(RUFF) check --fix .

migrate:
	@if docker compose ps -q app 2>/dev/null | grep -q .; then \
		docker compose exec -e PYTHONPATH=/app app alembic upgrade head; \
	else \
		$(ALEMBIC) upgrade head; \
	fi

revision:
	$(ALEMBIC) revision --autogenerate -m "$(message)"

up:
	docker compose up -d

down:
	docker compose down

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
