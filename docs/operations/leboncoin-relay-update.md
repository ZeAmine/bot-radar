# Relais SOCKS5 — mise à jour sur le Pi

Statut : procédure opérationnelle. Elle s'exécute **sous mandat d'exploitation**,
par un humain, sur le Raspberry Pi du domicile. Ce document n'autorise rien et
n'est pas exécuté par l'assistant.

Le relais vit dans `scripts/leboncoin_residential_relay.py`. Il écoute en boucle
locale, refuse toute destination hors liste, plafonne les connexions simultanées
et borne la durée de chaque connexion.

## Valeurs en vigueur

| Option | Défaut | Rôle |
| --- | --- | --- |
| `--host` | loopback | Adresse d'écoute ; hors loopback refusé sans option explicite |
| `--port` | port documenté dans la configuration de déploiement | Port d'écoute |
| `--allowed-domain` | `leboncoin.fr` | Répétable ; domaine et sous-domaines |
| `--max-connections` | `8` | Connexions simultanées |
| `--max-connection-seconds` | `120` | Durée totale par connexion |
| `--allow-non-loopback` | absent | Option explicite pour écouter hors loopback |

Une valeur invalide (plafond non entier ou nul, durée nulle, négative, non finie)
fait **refuser le démarrage**. C'est voulu : un démarrage silencieux avec des
garde-fous désactivés est pire qu'un refus.

## 1. Préchecks — avant tout redémarrage

À exécuter sans modifier l'état.

| # | Contrôle | Commande (paramètres entre chevrons) | Attendu |
| --- | --- | --- | --- |
| 1.1 | Processus relais en cours | liste des processus filtrée sur le relais | Présent, avec sa ligne de commande complète |
| 1.2 | Version en cours | lecture de la ligne de commande (chemin et options) | Options et valeurs connues |
| 1.3 | Écoute locale | `ss -H -ltn` filtré sur le port | Une écoute sur l'adresse attendue |
| 1.4 | Journal récent | lecture des dernières lignes du journal du relais | Aucune erreur répétée, pas de refus en rafale |
| 1.5 | Tunnel | liste des processus du client SSH | Tunnel ouvert |
| 1.6 | Espace disque | `df -h` sur la partition des journaux | Marge suffisante |
| 1.7 | Contrôle de sortie actuel | ligne de base de l'IP publique observée, **notée hors rapport** | Sortie résidentielle |

> Le contrôle 1.7 sert à comparer **avant / après**. La valeur elle-même ne se
> recopie dans aucun rapport partagé.

## 2. Déploiement

Le Pi n'est pas un dépôt Git dans la procédure documentée : le code est copié.
Vérifier d'abord que le contenu copié correspond au commit mergé sur `main`.

| # | Étape | Détail |
| --- | --- | --- |
| 2.1 | Identifier le commit de référence | Le SHA de `main` après merge de la PR du relais |
| 2.2 | Relever l'état précédent | Copier ou renommer le fichier du relais en place avant remplacement |
| 2.3 | Copier le nouveau fichier | Même emplacement, mêmes permissions |
| 2.4 | Vérifier le fichier copié | Empreinte identique à la source |
| 2.5 | Relire la ligne de commande | Options identiques à avant, sauf changement voulu |
| 2.6 | Redémarrer le relais | Selon le mécanisme de lancement en place |
| 2.7 | Constater le démarrage | Processus présent, aucune erreur au démarrage |

Un refus de démarrage avec un message explicite (plafond, durée, domaine) est un
**résultat attendu** si l'option est réellement invalide : corriger l'option, ne
pas désactiver le contrôle.

## 3. Vérification du journal

| # | Contrôle | Attendu |
| --- | --- | --- |
| 3.1 | Message de démarrage | Présent, une seule fois |
| 3.2 | Erreurs au démarrage | Aucune trace d'exception |
| 3.3 | Refus de plafond | Message de refus **uniquement** si le plafond a réellement été atteint |
| 3.4 | Refus de destination | Message de refus avec le **domaine** refusé, jamais une adresse interne |
| 3.5 | Échéances | Message de fermeture à l'échéance uniquement pour de vraies connexions longues |

Un flux normal ne doit produire **aucun** message de refus. Des refus répétés
signalent soit un plafond trop bas, soit une destination légitime manquante dans
la liste.

## 4. Tests avec les vraies destinations, sous mandat

Cette section établit la liste réellement nécessaire. Elle ne peut pas être
remplie depuis le dépôt.

| # | Test | Attend |
| --- | --- | --- |
| 4.1 | Requête vers le domaine principal par le relais | Réponse conforme, pas de refus de destination |
| 4.2 | Requête vers chaque sous-domaine observé | Réponse conforme ou refus explicite à documenter |
| 4.3 | Requête vers les autres domaines réellement sollicités | Refus explicite si absent de la liste |
| 4.4 | Redirections effectives | Aucune destination légitime ne doit être refusée |

Chaque refus observé doit être qualifié : **légitime** (le domaine n'était pas
nécessaire) ou **manquant** (il faut l'ajouter en option explicite). Un refus
« manquant » se corrige par une option `--allowed-domain` supplémentaire,
jamais en élargissant le motif à un domaine parent.

### 4.5 Contrôle volontaire de sortie

Objectif : prouver que le contrôle de sortie **fonctionne** et qu'il n'est pas
silencieusement inactif.

| # | Étape | Attend |
| --- | --- | --- |
| a | Ajouter **explicitement** un domaine de contrôle à la liste, uniquement pour le test | Option supplémentaire visible dans la ligne de commande |
| b | Redémarrer le relais | Démarrage normal |
| c | Effectuer une requête vers ce domaine de contrôle | Réponse reçue : le contrôle de sortie est opérant |
| d | Effectuer une requête vers un domaine **hors** liste | Refus explicite, journalisé |
| e | **Retirer** le domaine de contrôle | — |
| f | Redémarrer et confirmer le refus du domaine de contrôle | Refus : la liste est bien restrictive |
| g | Confirmer que le service nominal fonctionne toujours | Réponse conforme |

**Ne jamais conserver le domaine de contrôle** après le test. Le test échoue s'il
laisse une destination permanente ajoutée sans justification.

## 5. Rollback

| # | Étape | Attend |
| --- | --- | --- |
| 5.1 | Arrêter le relais en cours | Processus absent |
| 5.2 | Restaurer le fichier précédent (relevé en 2.2) | Empreinte identique à l'original |
| 5.3 | Redémarrer avec la ligne de commande précédente | Démarrage normal |
| 5.4 | Rejouer le contrôle de sortie (1.7) | Même sortie qu'avant la mise à jour |
| 5.5 | Rejouer une requête nominale | Réponse conforme |
| 5.6 | Consigner l'incident | Cause, heure, commit abandonné |

Le rollback est **complet** quand l'écoute, la sortie résidentielle et le service
nominal sont revenus à l'état mesuré en section 1.

## 6. Critères d'échec de la mise à jour

Interrompre et revenir en arrière si, après déploiement :

- le relais refuse de démarrer alors que la ligne de commande est valide ;
- des destinations légitimes sont refusées et ne peuvent pas être ajoutées sans
  élargir au domaine parent ;
- la sortie n'est plus résidentielle ;
- le journal montre des erreurs répétées non expliquées ;
- des connexions dépassent la durée plafond dans le journal.

## 7. Point de vigilance — DNS

La résolution DNS n'est **pas** bornée par la durée maximale de connexion : elle
n'expose aucun descripteur interruptible. Un résolveur lent peut donc retarder un
worker au-delà de l'échéance et garder sa place occupée. Le plafond de connexions
borne l'épuisement, mais une résolution bloquée de façon persistante doit être
corrigée côté système (résolveur), pas en élargissant le plafond.
