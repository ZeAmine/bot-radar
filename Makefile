.PHONY: help dev test test-isolated lint bench check-history format migrate revision up down clean \
        lock check-lock preflight verify-deploy deploy pr verify

# Outils : venv si présent sinon PATH ; repli sur le nom nu du binaire (erreur lisible plutôt qu'une commande tronquée).
PYTHON  ?= $(shell test -x .venv/bin/python  && echo .venv/bin/python  || echo python3)
RUFF    ?= $(shell test -x .venv/bin/ruff    && echo .venv/bin/ruff    || echo ruff)
MYPY    ?= $(shell test -x .venv/bin/mypy    && echo .venv/bin/mypy    || echo mypy)
PYTEST  ?= $(shell test -x .venv/bin/pytest  && echo .venv/bin/pytest  || echo pytest)
ALEMBIC ?= $(shell test -x .venv/bin/alembic && echo .venv/bin/alembic || echo alembic)
UVICORN ?= $(shell test -x .venv/bin/uvicorn && echo .venv/bin/uvicorn || echo uvicorn)
GIT    ?= git

help:
	@echo "Commandes disponibles pour BotRadar :"
	@echo "  make dev                 Lancer le serveur de développement FastAPI"
	@echo "  make test                Lancer les tests unitaires et d'intégration"
	@echo "  make test-isolated       Suite complète, configuration factice et transports isolés"
	@echo "  make bench               Benchmark du chemin d'alerte (invariant 500 ms)"
	@echo "  make lint                Vérifier le code avec Ruff et Mypy"
	@echo "  make lock                Régénérer requirements.lock depuis l'environnement courant"
	@echo "  make check-lock          Vérifier que le verrou couvre pyproject.toml"
	@echo "  make check-history       Rejouer Ruff et Pytest sur chaque commit (Mypy sur option)"
	@echo "  make preflight           Vérifier le VPS avant une synchronisation"
	@echo "  make verify-deploy       Revérifier le VPS après une synchronisation"
	@echo "  make deploy              Déployer (pré-vol → arrêt confirmé → synchro → build → migration → démarrage → contrôle)"
	@echo "  make pr                  Branche, commit, push et PR (MESSAGE=… ou MESSAGE_FILE=… TITLE=… [BRANCH=…] [STAGE=…])"
	@echo "  make verify              Attendre la CI verte (contrôles locaux selon AGENTS.md)"
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

# Réutilise la recette existante ; aucune sélection PostgreSQL ou sauvegarde implicite.
test-isolated:
	$(PYTHON) -B scripts/verify_consolidation.py unit --target tests

bench:
	$(PYTEST) tests/performance -v -s

lint:
	$(RUFF) check .
	$(MYPY) app scripts/matching_diagnostics.py

# check-lock : hors réseau, échoue si une dépendance est ajoutée sans être épinglée dans pyproject.toml.
check-lock:
	$(PYTHON) scripts/check_lock.py

# lock : régénérer après modification de pyproject.toml ; Dockerfile et CI lisent ce fichier (en-tête conservé).
lock:
	@grep "^#" requirements.lock > requirements.lock.new
	@$(PYTHON) -m pip freeze \
		| grep -v "^-e " \
		| grep -viE "^(botradar|radar_bot|radar-bot)==" \
		| grep -E "^[A-Za-z0-9._-]+==" \
		| sort -f >> requirements.lock.new
	@mv requirements.lock.new requirements.lock
	@$(MAKE) --no-print-directory check-lock

# check-history : rejeu par commit dans worktree jetable, du plus ancien au plus récent (BASE, STEPS).
check-history:
	$(PYTHON) scripts/check_history.py $(if $(BASE),--base $(BASE),) \
		$(if $(STEPS),--steps $(STEPS),) $(if $(MAX_COMMITS),--max-commits $(MAX_COMMITS),)

# preflight/verify-deploy : encadrent la sync rsync pour rendre l'écart visible (push ne déploie rien).
preflight:
	$(PYTHON) scripts/preflight_deploy.py

verify-deploy:
	$(PYTHON) scripts/preflight_deploy.py --post-sync

# deploy : procédure complète dans l'ordre, s'arrêtant au premier échec ; reprise manuelle possible.
deploy:
	$(PYTHON) scripts/deploy.py $(if $(DEPLOY_ARGS),$(DEPLOY_ARGS),)

# pr : branche puis PR, gestes enchaînés jusqu'au premier échec (push direct sur main refusé).
# Le commit porte sur STAGE (pas git add -A) pour ne pas balayer le travail d'autres tâches.
pr:
	@if [ -z "$(TITLE)" ] || { [ -z "$(MESSAGE)" ] && [ -z "$(MESSAGE_FILE)" ]; }; then \
		echo "usage: make pr (MESSAGE=\"...\" | MESSAGE_FILE=<chemin>) TITLE=\"...\" [BRANCH=<nom>] [STAGE=\"chemins\"]"; \
		exit 2; \
	fi
	@branch="$(if $(BRANCH),$(BRANCH),feat/$(shell date +%Y%m%d-%H%M))"; \
	case "$$branch" in \
		feat/*|fix/*|docs/*|chore/*) ;; \
		*) echo "BRANCH doit commencer par feat/, fix/, docs/ ou chore/ (reçu : $$branch)"; exit 2 ;; \
	esac; \
	echo "Fichiers retenus pour le commit :"; \
	if [ -n "$(STAGE)" ]; then \
		for path in $(STAGE); do \
			if [ -z "$$($(GIT) status --porcelain -- "$$path")" ]; then \
				echo "  rien à indexer pour $$path : chemin inconnu, ou deja à l'identique."; \
				exit 2; \
			fi; \
			if $(GIT) add -A -- "$$path" 2>/dev/null; then :; \
			else \
				echo "  $$path : suppression déjà indexée, retrait de l'index."; \
				$(GIT) update-index --remove -- "$$path" || exit 1; \
			fi; \
		done; \
	else \
		echo "  STAGE est vide : la cible ne devine pas le périmètre d'un commit."; \
		echo "  Modifications et fichiers non suivis présents dans l'arbre :"; \
		$(GIT) status --short | sed 's/^/    /'; \
		echo "  Relancez avec STAGE=\"<chemins>\" pour nommer ce qui doit partir."; \
		exit 2; \
	fi; \
	if [ "$(BRANCH)" ]; then \
		if [ "$$($(GIT) rev-parse --abbrev-ref HEAD)" != "$$branch" ]; then \
			$(GIT) switch "$$branch" 2>/dev/null || $(GIT) switch -c "$$branch" || exit 1; \
		fi; \
	else \
		$(GIT) switch -c "$$branch"; \
	fi; \
	$(GIT) status --short; \
	if [ -n "$(MESSAGE_FILE)" ]; then \
		$(GIT) commit -F "$(MESSAGE_FILE)" || exit 1; \
	else \
		$(GIT) commit -m "$(MESSAGE)" || exit 1; \
	fi; \
	$(GIT) push -u origin "$$branch" || exit 1; \
	if [ -n "$(MESSAGE_FILE)" ]; then \
		gh pr create --base main --head "$$branch" --title "$(TITLE)" --body-file "$(MESSAGE_FILE)"; \
	else \
		gh pr create --base main --head "$$branch" --title "$(TITLE)" --body "$$(printf '%s\n\n🤖 Generated with Codebuff\nCo-Authored-By: Codebuff <noreply@codebuff.com>' '$(MESSAGE)')"; \
	fi

verify:
	@branch="$$($(GIT) rev-parse --abbrev-ref HEAD)"; \
	echo "Attente de la CI sur $$branch…"; \
	gh pr checks "$$branch" --watch --fail-fast

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
