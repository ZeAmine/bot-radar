# Décisions de BotRadar

Statut : index de décisions historiques, avec leur applicabilité actuelle.
Périmètre : raisons et conséquences ; les contrats liés portent le comportement courant.

Une date « introduction Git » prouve la présence de la déclaration dans le dépôt,
pas la date de la décision. Le SHA de première déclaration date uniquement son introduction. Le texte complet
avant consolidation, amendements compris, reste dans Git au commit `6292e2e`.
Les performances, sondes réseau et états de production anciens ne sont pas des garanties.

Le test `tests/unit/test_adr_sequence.py` lit tous les recueils Markdown de ce dossier
hors cet index : une seule déclaration par numéro, séquence continue depuis ADR-001.
Conserver les identifiants et expliciter tout remplacement plutôt que renuméroter.

| Identifiant | Décision | Recueil |
| --- | --- | --- |
| ADR-001 | Monolithe asynchrone à processus unique (`radar_app`) | [architecture](architecture.md#adr-001) |
| ADR-002 | Invariant n°1 — Alerte Telegram d'abord, I/O réseau différées | [architecture](architecture.md#adr-002) |
| ADR-003 | Matching déterministe local en mémoire CPU | [architecture](architecture.md#adr-003) |
| ADR-004 | Cache local de cote BD avec rafraîchissement bi-quotidien | [architecture](architecture.md#adr-004) |
| ADR-005 | Rôle purement consultatif de l'IA (DeepSeek) | [architecture](architecture.md#adr-005) |
| ADR-006 | Suppression de la détection des défauts cachés ("Hidden Condition") | [telegram-matching](telegram-matching.md#adr-006) |
| ADR-007 | Suppression du bouton "Payer" (`[ 💳 Payer ]`) | [telegram-matching](telegram-matching.md#adr-007) |
| ADR-008 | Auto-Tuning conversationnel & Purge des faux positifs | [telegram-matching](telegram-matching.md#adr-008) |
| ADR-009 | Isolation stricte des sessions de base de données en arrière-plan | [architecture](architecture.md#adr-009) |
| ADR-010 | Gestion tolérante des séries BD spéciales (ex: 1629, RIP, Blast) | [catalogue-market](catalogue-market.md#adr-010) |
| ADR-011 | Purge des use cases parasites et modules IA inadaptés | [architecture](architecture.md#adr-011) |
| ADR-012 | Migration vers l'API interne JSON Vinted (`svc-catalogue`) & Cadence Adaptative | [marketplace](marketplace.md#adr-012) |
| ADR-013 | Passerelle centralisée multi-bots (caddy_gateway) et harmonisation VPS ~/bot-radar/ | [architecture](architecture.md#adr-013) |
| ADR-014 | La BNF est la seule autorité ISBN (« l'IA propose, la BNF dispose ») | [catalogue-market](catalogue-market.md#adr-014) |
| ADR-015 | Suppression de l'album d'images (l'alerte garde sa couverture) | [telegram-matching](telegram-matching.md#adr-015) |
| ADR-016 | Un seul filtre d'occasion, une seule référence de prix | [catalogue-market](catalogue-market.md#adr-016) |
| ADR-017 | Choisir son édition dans le récap, avant de créer l'alerte | [catalogue-market](catalogue-market.md#adr-017) |
| ADR-018 | Le fournisseur IA est un adaptateur, jamais une dépendance | [architecture](architecture.md#adr-018) |
| ADR-019 | Une cote « prix le plus bas constaté » bornée par nos propres relevés | [catalogue-market](catalogue-market.md#adr-019) |
| ADR-020 | Une cote qu'on ne nourrit plus se signale, et la session se réinjecte | [catalogue-market](catalogue-market.md#adr-020) |
| ADR-021 | Un homonyme n'est pas l'œuvre, et changer d'ISBN change d'ouvrage | [catalogue-market](catalogue-market.md#adr-021) |
| ADR-022 | Libellé gras, consigne en italique, avertissement isolé | [telegram-matching](telegram-matching.md#adr-022) |
| ADR-023 | Une alerte BD ne part jamais sans bloc de cote | [catalogue-market](catalogue-market.md#adr-023) |
| ADR-024 | Une variante qui énumère n'est pas une variante, une exclusion n'est pas un chiffre | [telegram-matching](telegram-matching.md#adr-024) |
| ADR-025 | Une œuvre numérotée est une série, pas un tome | [catalogue-market](catalogue-market.md#adr-025) |
| ADR-026 | Une série se compose par ses tomes, et la mesure se fait à l'envoi | [catalogue-market](catalogue-market.md#adr-026) |
| ADR-027 | Une exclusion se qualifie par le titre, une variante ne le répète pas | [telegram-matching](telegram-matching.md#adr-027) |
| ADR-028 | L'identifiant se lit, il ne se devine pas — et le récap dit quand il manque | [catalogue-market](catalogue-market.md#adr-028) |
| ADR-029 | Un libellé de trois lettres, un marqueur qui ferme la ligne | [telegram-matching](telegram-matching.md#adr-029) |
| ADR-030 | Une exclusion n'est jamais le titre plus un numéro | [telegram-matching](telegram-matching.md#adr-030) |
| ADR-031 | L'indication de série sort de la liste des informations | [telegram-matching](telegram-matching.md#adr-031) |
| ADR-032 | Le récap de création et la confirmation sont une seule carte | [catalogue-market](catalogue-market.md#adr-032) |
| ADR-033 | Une troisième autorité, et l'aveu en tête de carte | [catalogue-market](catalogue-market.md#adr-033) |
| ADR-034 | Le jeton Chasse se renouvelle seul, sur l'hôte, et un doute déclenche la visite | [catalogue-market](catalogue-market.md#adr-034) |
| ADR-035 | L'ISBN exact prouve l'identité, la graphie du titre ne vétote plus | [catalogue-market](catalogue-market.md#adr-035) |
| ADR-036 | Le jeton Chasse vient du compte, pas d'un navigateur | [catalogue-market](catalogue-market.md#adr-036) |
| ADR-037 | Le total de série s'écrit à chaque cote, et chaque tome a sa ligne | [catalogue-market](catalogue-market.md#adr-037) |
| ADR-038 | Telegram consomme le contrat partagé, pour les nouvelles créations seulement | [catalogue-market](catalogue-market.md#adr-038) |
| ADR-039 | Une passe qui n'a rien trouvé a un résultat, et il se dit | [catalogue-market](catalogue-market.md#adr-039) |
| ADR-040 | Une série se complète par une seconde autorité, et rattrape ses tomes manquants | [catalogue-market](catalogue-market.md#adr-040) |
| ADR-041 | Une série que personne ne catalogue a une réponse, et elle se dit | [catalogue-market](catalogue-market.md#adr-041) |
| ADR-042 | Le marché se consulte à la demande, une tentative n'est pas un relevé | [catalogue-market](catalogue-market.md#adr-042) |
| ADR-043 | La création prépare les cotes, le panneau marché les présente sans doublons | [catalogue-market](catalogue-market.md#adr-043) |
| ADR-044 | Changer d’édition réaligne la fiche ; une annonce décrit la cote à l’envoi | [catalogue-market](catalogue-market.md#adr-044) |
| ADR-045 | Les homonymes ne doivent pas masquer les replis d'une nouveauté | [catalogue-market](catalogue-market.md#adr-045) |
| ADR-046 | La préparation BD porte une intention durable et une version | [catalogue-market](catalogue-market.md#adr-046) |
| ADR-047 | Le bouton « Acheter » prépare un checkout à la demande, jamais un paiement — retiré par ADR-051 | [marketplace](marketplace.md#adr-047) |
| ADR-048 | Le parcours local assisté — un marqueur, un clic, rien d'autre | [marketplace](marketplace.md#adr-048) |
| ADR-049 | Aucun userscript, aucune extension, aucun script injecté — le seul parcours est intégré au bot | [marketplace](marketplace.md#adr-049) |

| ADR-050 | Nouveautés au premier scan et minimum annuel unique vérifiable | [catalogue-market](catalogue-market.md#adr-050) |
| ADR-051 | Le checkout Vinted automatisé est retiré | [marketplace](marketplace.md#adr-051) |
| ADR-052 | Leboncoin entre dans les alertes, avec une sortie résidentielle auto-hébergée | [marketplace](marketplace.md#adr-052) |
| ADR-053 | Les seuils de mesure Leboncoin sont pré-enregistrés, sans oracle de rappel | [marketplace](marketplace.md#adr-053) |
| ADR-054 | Exception Leboncoin à la clause anti-contournement | [marketplace](marketplace.md#adr-054) |
