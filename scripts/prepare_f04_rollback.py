"""Produit un retour compatible : app parent, graphe cible, garde Telegram.

Ne déploie rien, ne contient aucun .env et ne remplace jamais un répertoire existant.
"""

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

DOCKERFILE = """ARG RETURN_RUNTIME_IMAGE
FROM ${RETURN_RUNTIME_IMAGE}
USER root
RUN rm -rf /app/app /app/scripts /app/alembic
WORKDIR /app
COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup scripts/ ./scripts/
COPY --chown=appuser:appgroup alembic/ ./alembic/
COPY --chown=appuser:appgroup alembic.ini ./alembic.ini
USER appuser
CMD ["python", "scripts/serve_f04_rollback.py"]
"""

ROLLBACK = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 5 ]; then
  echo "usage: rollback.sh PROJECT_DIR RUNTIME_IMAGE MAINTENANCE_CONFIG ORIGIN --execute" >&2
  exit 2
fi
project=$1
runtime=$2
maintenance=$3
origin=${4%/}
[ "$5" = --execute ] || exit 2
bundle=$(cd "$(dirname "$0")" && pwd)
[ -f "$project/compose.yml" ] || exit 2
cd "$project"
current_cid=$(docker compose ps -q app)
[ -n "$current_cid" ]
current_image=$(docker inspect --format '{{.Config.Image}}' "$current_cid")
current_id=$(docker inspect --format '{{.Image}}' "$current_cid")
runtime_id=$(docker image inspect --format '{{.Id}}' "$runtime")
[ "$runtime_id" = "$current_id" ]
# L'image doit déjà être présente : aucun pull/installation.
docker image inspect "$runtime" >/dev/null
docker build --pull=false --network=none --build-arg "RETURN_RUNTIME_IMAGE=$runtime" \
  -f "$bundle/Dockerfile.rollback" -t bot-radar-app:f04-compatible-return "$bundle"
# Configuration complète préparée et revue ; aucun ancien update ne doit entrer.
docker exec caddy_gateway caddy validate --config "$maintenance"
docker exec caddy_gateway caddy reload --config "$maintenance"
[ "$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' "$origin/health")" = 200 ]
probe="$origin/api/v1/telegram/webhook/__maintenance_probe__"
[ "$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$probe")" = 503 ]
cd "$project"
docker compose stop app
cid=$(docker compose ps -aq app)
[ -n "$cid" ]
[ "$(docker inspect --format '{{.State.Running}}' "$cid")" = false ]
# One-shot de maintenance : aucun app.main, scheduler ou poller.
docker compose run --rm --no-deps -T --entrypoint python \
  -v "$bundle/scripts/freeze_bd_preparations.py:/rollback/freeze.py:ro" \
  app /rollback/freeze.py --execute --confirmed-stopped > /tmp/f04-frozen-state.json
# Restaurer seulement le code ; .env et données restent hors cible.
orphan_before=absent
if [ -f scripts/push_protected.py ]; then orphan_before=$(sha256sum scripts/push_protected.py); fi
rsync -a --delete "$bundle/app/" app/
rsync -a --delete --exclude=push_protected.py "$bundle/scripts/" scripts/
if [ "$orphan_before" != absent ]; then
  [ "$(sha256sum scripts/push_protected.py)" = "$orphan_before" ]
fi
docker tag bot-radar-app:f04-compatible-return "$current_image"
docker compose up -d --no-build --pull never --force-recreate --no-deps app
cid=$(docker compose ps -q app)
state=starting
for attempt in $(seq 1 30); do
  state=$(docker inspect --format '{{.State.Health.Status}}' "$cid")
  [ "$state" = healthy ] && break
  sleep 2
done
[ "$state" = healthy ]
[ "$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' "$origin/health")" = 200 ]
[ "$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$probe")" = 503 ]
docker compose exec -T app alembic current | grep '^0010_alert_preparation (head)'
docker compose exec -T app alembic heads | grep '^0010_alert_preparation (head)'
services=$(docker compose ps --services --status running)
if printf '%s\n' "$services" | grep -qi worker; then exit 1; fi
docker cp "$bundle/manifest.json" "$cid:/tmp/f04-return-manifest.json"
docker exec -i -w /app "$cid" python - <<'PY'
import hashlib, json, os
from pathlib import Path
m = json.loads(Path('/tmp/f04-return-manifest.json').read_text())
names = [n for n in m['files']
         if n.startswith(('app/', 'scripts/', 'alembic/')) or n == 'alembic.ini']
assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest() == m['files'][n] for n in names)
processes = [(p / 'comm').read_text().strip() for p in Path('/proc').glob('[0-9]*')
             if p.name != str(os.getpid()) and (p / 'comm').is_file()]
assert sum(n.startswith('python') or n == 'uvicorn' for n in processes) == 1
print('Empreintes et processus unique conformes')
PY
created=$(docker inspect --format '{{.Created}}' "$cid")
[ "$(docker inspect --format '{{.RestartCount}}' "$cid")" = 0 ]
docker logs --since "$created" "$cid" > /tmp/f04-return-startup.log 2>&1
python3 - <<'PY'
import json
from pathlib import Path
started = 0
for line in Path('/tmp/f04-return-startup.log').read_text().splitlines():
    assert 'Traceback (most recent call last)' not in line
    assert not line.startswith(('ERROR:', 'CRITICAL:'))
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    assert record.get('level') not in ('ERROR', 'CRITICAL')
    started += (record.get('logger') == 'apscheduler.scheduler'
                and record.get('message') == 'Scheduler started')
assert started == 1
print('Démarrage et scheduler unique conformes')
PY
docker compose exec -T app python scripts/freeze_bd_preparations.py \
  > /tmp/f04-after-return-state.json
python3 - <<'PY'
import json
a = json.load(open('/tmp/f04-frozen-state.json'))
b = json.load(open('/tmp/f04-after-return-state.json'))
assert a['marker_hash'] == b['marker_hash'] and b['requests_to_freeze'] == 0
print('Marqueurs conservés et intentions retirées')
PY
# Ne pas déclarer faussement le parent : graphe et garde font partie de ce build.
cp "$bundle/manifest.json" .return_release_manifest.json
echo "Retour vérifié sous maintenance. Ne pas rouvrir Telegram avec ce code ancien."
"""


def prepare(base: str, target: str, output: Path) -> dict[str, object]:
    """Matérialise les sources et leur manifeste sans toucher au déploiement."""
    root = Path(__file__).resolve().parents[1]
    base = subprocess.check_output(["git", "rev-parse", base], cwd=root, text=True).strip()
    target = subprocess.check_output(["git", "rev-parse", target], cwd=root, text=True).strip()
    output.mkdir(parents=True, exist_ok=False)
    for ref, paths in (
        (base, ["app", "scripts", "alembic", "alembic.ini"]),
        (target, ["alembic", "alembic.ini"]),
    ):
        archive = subprocess.check_output(["git", "archive", ref, *paths], cwd=root)
        with tarfile.open(fileobj=io.BytesIO(archive)) as content:
            content.extractall(output, filter="data")
    for name in ("freeze_bd_preparations.py", "serve_f04_rollback.py"):
        (output / "scripts" / name).write_bytes((root / "scripts" / name).read_bytes())
    (output / "Dockerfile.rollback").write_text(DOCKERFILE)
    runner = output / "rollback.sh"
    runner.write_text(ROLLBACK)
    runner.chmod(0o755)
    files = {
        p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    manifest: dict[str, object] = {"base_commit": base, "migration_commit": target, "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.base, args.target, args.output)
    print(
        json.dumps(
            {
                "base_commit": result["base_commit"],
                "migration_commit": result["migration_commit"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
