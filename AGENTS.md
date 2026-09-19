# Instructions du projet Radar Bot & Accès Production

## Environnement VPS de Production

Le projet est déployé en production 24h/24 sur une instance Oracle Cloud Free Tier.

- **Hôte & Utilisateur** : `ubuntu@88.96.56.44`
- **Clé SSH** : `~/.ssh/ssh-key-2026-09-10.key`
- **Répertoire distant** : `~/radar-bot/`
- **Domaine public** : `https://radar.aminezegmou.fr`
- **Stack Docker Compose** :
  - `radar_app` : API FastAPI, webhook Telegram, APScheduler et Vinted Poller unifiés (port 8000)
  - `radar_postgres` : PostgreSQL 16
  - `radar_redis` : Redis 7
  - `radar_caddy` : Reverse proxy HTTPS automatique

## Directive d'exécution autonome

Pour toute action sur le serveur de production (déploiement de code, synchronisation de fichiers, migration de base de données, redémarrage ou rebuild de conteneurs, diagnostics et consultation des logs) :

1. **Exécution directe** : L'assistant doit exécuter lui-même les commandes nécessaires via l'outil `run_command` (avec `BypassSandbox: true` pour autoriser l'accès réseau et la clé SSH), plutôt que de demander à l'utilisateur de copier-coller des commandes dans son terminal.
2. **Synchronisation de fichiers** :
   ```bash
   rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.git' \
     -e "ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no" \
     ./ ubuntu@88.96.56.44:~/radar-bot/
   ```
3. **Commandes distantes via SSH** :
   ```bash
   ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 "<commande>"
   ```
4. **Rebuild / Redémarrage Docker** :
   ```bash
   ssh -i ~/.ssh/ssh-key-2026-09-10.key -o StrictHostKeyChecking=no ubuntu@88.96.56.44 "cd ~/radar-bot && docker compose up -d --build app"
   ```

## Directive Qualité de Code & Réactivité

1. **Zéro code mort ou inactif** : Remplacer, améliorer et corriger le code existant de manière propre et intelligente sans laisser de blocs commentés ou orphelins.
2. **Préservation stricte de la réactivité** : Toujours peser le pour et le contre sur chaque modification. Aucune logique ne doit ralentir la détection ou l'envoi instantané de l'alerte Vinted (les calculs lourds ou requêtes tierces lentes doivent être en tâche de fond ou sous cache strict).

