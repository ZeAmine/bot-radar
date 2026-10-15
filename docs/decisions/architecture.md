# Décisions — architecture

Statut : décisions datées ; vérifier le statut de chaque entrée.
Périmètre : contexte et raisons de ce thème, sans procédure d’exploitation implicite.
Contrat/code de référence : [lecture spécialisée](../../app/main.py).

<a id="adr-001"></a>

## ADR-001 : Monolithe asynchrone à processus unique (`radar_app`)

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : API, bot et tâches doivent tenir sur le même hôte.
- Décision et raison : Un monolithe asyncio dans radar_app réduit la coordination distribuée ; le lifespan porte les producteurs.
- Conséquences : Aucun second worker/scheduler. Les chiffres de mémoire anciens restent des observations historiques, pas un budget garanti.

<a id="adr-002"></a>

## ADR-002 : Invariant n°1 — Alerte Telegram d'abord, I/O réseau différées

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Les enrichissements réseau retardaient la décision utilisateur.
- Décision et raison : Envoyer après matching CPU et cache local, puis enrichir avec le vrai message_id dans des tâches retenues et des sessions séparées.
- Conséquences : ADR-023/039/041/044 précisent les lignes d’absence : « sans cote » ne signifie jamais sans bloc explicatif.

<a id="adr-003"></a>

## ADR-003 : Matching déterministe local en mémoire CPU

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Le filtrage doit être explicable et indépendant du moteur IA.
- Décision et raison : Les règles pures locales déterminent les matchs ; ni vecteurs ni LLM sur ce chemin.
- Conséquences : Les durées simulées ne prouvent ni latence réelle ni absence générale de faux positifs.

<a id="adr-004"></a>

## ADR-004 : Cache local de cote BD avec rafraîchissement bi-quotidien

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : La collecte de prix ne doit pas dépendre de chaque alerte.
- Décision et raison : Lire exclusivement le cache pendant l’envoi ; actualiser en tâche planifiée et par le chemin central de cotation.
- Conséquences : Les heures sont configurables ; ADR-042/043 ajoutent collecte à la demande et préparation avant surveillance.

<a id="adr-005"></a>

## ADR-005 : Rôle purement consultatif de l'IA (DeepSeek)

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Un moteur peut échouer, expirer ou rendre un JSON inutilisable.
- Décision et raison : L’IA reste consultative et ne bloque pas l’alerte ; les résultats portent le fait answered pour distinguer panne et avis vide.
- Conséquences : La création peut réussir sans suggestion ; l’interface explique pourquoi. ADR-018 rend le fournisseur interchangeable.

<a id="adr-009"></a>

## ADR-009 : Isolation stricte des sessions de base de données en arrière-plan

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Partager une session SQLAlchemy avec le fond causait concurrence et fermeture prématurée.
- Décision et raison : Chaque tâche de fond ouvre sa propre session ; le parent ne lui prête pas sa transaction.
- Conséquences : Relire les objets requis dans la session courante au lieu de rattacher un objet venu de la cotation.

<a id="adr-011"></a>

## ADR-011 : Purge des use cases parasites et modules IA inadaptés

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Plusieurs modules expérimentaux doublaient le matching ou inventaient des prix sans données.
- Décision et raison : Retirer vision guard, décomposition IA de lots, price glitch et boutons/flags orphelins ; réutiliser les règles déterministes.
- Conséquences : Aucun chemin parallèle « bientôt ». Les suppressions historiques de tests ne constituent pas une permission de suppression actuelle.

<a id="adr-013"></a>

## ADR-013 : Passerelle centralisée multi-bots (caddy_gateway) et harmonisation VPS ~/bot-radar/

Introduction Git : 2026-09-27 · Statut : **accepté**. Première déclaration : `85bed73f26d6`.

- Contexte : Plusieurs applications partagent les ports HTTPS du VPS.
- Décision et raison : Centraliser Caddy hors dépôt sur web_gateway et livrer BotRadar dans ~/bot-radar/.
- Conséquences : Ne pas recréer radar_caddy local. Les anciennes opérations sur volumes et symlinks sont historiques ; la procédure actuelle reste le script de déploiement.

<a id="adr-018"></a>

## ADR-018 : Le fournisseur IA est un adaptateur, jamais une dépendance

Introduction Git : 2026-10-02 · Statut : **accepté**. Première déclaration : `35d455f4932a`.

- Contexte : Changer de fournisseur touchait handlers, cache, exceptions et configuration.
- Décision et raison : Isoler le dialecte dans providers/llm ; BaseLLMClient porte la sémantique, le registre choisit l’adaptateur et refuse un fournisseur inconnu.
- Conséquences : Client injecté, cache incluant fournisseur/modèle/version de prompt, contrat commun testé sur chaque adaptateur. Aucun shim DEEPSEEK historique.
