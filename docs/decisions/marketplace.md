# Décisions — marketplace

Statut : décisions datées ; vérifier le statut de chaque entrée.
Périmètre : contexte et raisons de ce thème, sans procédure d’exploitation implicite.
Contrat/code de référence : retiré — voir [ADR-051](#adr-051).

<a id="adr-012"></a>

## ADR-012 : Migration vers l'API interne JSON Vinted (`svc-catalogue`) & Cadence Adaptative

Introduction Git : 2026-09-24 · Statut : **accepté**. Première déclaration : `898d613d8fda`.

- Contexte : Les catalogues HTML lourds et les visites individuelles coûtaient réseau et délai.
- Décision et raison : Interroger le catalogue JSON avec repli HTML, couverture intégrée et cadence adaptative entrelacée.
- Conséquences : Évolution du 13 octobre : intervalle standard 12 s, paramètre servant de plafond aux accélérations. La sonde historique n’exposait pas published_at ; aucun délai publication→détection établi.

<a id="adr-047"></a>

## ADR-047 : Le bouton « Acheter » prépare un checkout à la demande, jamais un paiement

Date : 2026-10-14 · Statut : **remplacé par ADR-051**. Première déclaration : `6292e2efcf75`.

- Contexte : L’utilisateur autorise une préparation manuelle accélérée, sans achat autonome.
- Décision et raison : Au clic seulement, vérifier propriétaire et association à une alerte non supprimée, prérequis, session dédiée, verrou anti-double-clic et cache de lien ; préparer la transaction et le checkout.
- Conséquences : Amende ADR-007. Le paiement/relais restent manuels. Tout échec coupe la création pour la durée de sa cause (blocage/limitation 30 min, délai/réseau 15 min, autres 5 min) ; le verrou d'exécution est global et possédé par un jeton (seul son détenteur le libère), et la coupure est posée avant sa libération puis revérifiée dans le verrou. Un `400 INVALID_INPUT` ne déclenche aucun repli — son absence d'effet de bord n'est pas établie — ; seul un `404` (route absente) ouvre un repli unique vers le chemin historique, sans boucle. La session validée sur le corps n'est jamais exposée. Le fil vendeur ouvert est un effet réel ; risques contractuels assumés.
- Amendement du 16 octobre 2026 : l'ancien seuil d'échecs doux et le verrou conservé après un échec incertain ne s'appliquent plus. La concurrence reste limitée, avec une fenêtre résiduelle assumée et **sans** identifiant d'idempotence : une reprise explicite après un résultat incertain peut créer une seconde conversation.
- Observation historique du 14 octobre, source de consolidation `6292e2e` : refus anonyme et défi DataDome classés, suspension observée ; ce ne sont pas une réussite authentifiée ni une observation actuelle. Retiré par ADR-051.

<a id="adr-048"></a>

## ADR-048 : Le parcours local assisté — un marqueur, un clic, rien d'autre

Date : 2026-10-14 · Statut : **abandonné, remplacé par ADR-049**. Première déclaration : `6292e2efcf75`.

- Contexte : Un ancien userscript pouvait choisir un relais ou cliquer sans intention explicite.
- Décision et raison : Un remplacement avait limité le clic natif à un marqueur de lien, consommé une seule fois.
- Conséquences : Abandonné intégralement par ADR-049 le 14 octobre 2026, y compris marqueur, harnais et documentation d’installation. Ce récit n’autorise aucune réintroduction.

<a id="adr-049"></a>

## ADR-049 : Aucun userscript, aucune extension, aucun script injecté — le seul parcours est intégré au bot

Date : 2026-10-14 · Statut : **accepté**. Première déclaration : `6292e2efcf75`.

- Contexte : L’utilisateur retire toute méthode userscript, extension ou injection de page.
- Décision et raison : Supprimer le parcours local assisté d’ADR-048 ; seul subsiste le callback serveur d’ADR-047, derrière son interrupteur et avec lien manuel de repli.
- Conséquences : Remplace ADR-048. La réussite authentifiée de bout en bout reste non démontrée à la base de cette tâche. Un blocage n'autorise pas le retour à une méthode abandonnée. Le contournement des protections d'accès n'est plus interdit par `AGENTS.md` depuis le 20 octobre 2026 (ADR-054) ; cette décision ne l'autorisait pas et ne concerne pas le parcours acheteur décrit ici. ADR-051 retire le callback serveur d’ADR-047 ; l’interdiction des userscripts, extensions et scripts injectés reste en vigueur.
- Observation historique du 14 octobre, source de consolidation `6292e2e` : refus anonyme et défi DataDome classés, suspension observée ; ce ne sont pas une réussite authentifiée ni une observation actuelle.

<a id="adr-051"></a>

## ADR-051 : Le checkout Vinted automatisé est retiré

Date : 2026-10-17 · Statut : **accepté**. Remplace ADR-047.

- Contexte : L’utilisateur renonce à toute automatisation d’achat ; la préparation de checkout au clic est retirée du produit.
- Décision et raison : Supprimer le bouton « Acheter » et tout le chemin serveur (provider, cas d’usage, handler, session de compte injectée, route interne, script de capture) ; l’accès à l’annonce reste manuel par « Voir l’annonce ».
- Conséquences : ADR-047 devient caduque. ADR-048 et ADR-049 restent en vigueur et ne sont pas rouvertes : aucun userscript, aucune extension, aucun script injecté, et aucun remplacement automatisé n’est introduit. La réussite authentifiée de bout en bout d’ADR-047 n’était pas démontrée avant ce retrait ; elle ne l’est pas davantage après.
- Observation historique bornée : les refus/DataDome du 14 octobre 2026 et l’absence de lien d’achat partageable restent consignés dans les ADR datées de ce recueil.

<a id="adr-052"></a>

## ADR-052 : Leboncoin entre dans les alertes, avec une sortie résidentielle auto-hébergée

Date : 2026-10-19 · Statut : **accepté**.

- Contexte : les alertes BD ne scrutaient que Vinted. L'utilisateur veut les mêmes alertes sur Leboncoin, sans compte payant, sans SaaS et sans exécution continue sur son Mac. DataDome protège le site et son `robots.txt` interdit `/recherche` ainsi que l'usage de robots. L'utilisateur retire, pour l'ensemble du dépôt, la clause anti-contournement d'`AGENTS.md` (ADR-054) ; cette clause a disparu du fichier de règles le 20 octobre 2026 et les autres interdictions restent en vigueur.
- Décision et raison : collecteur dans le processus unique, `source_name="leboncoin"`, une page de recherche Livres filtrée BD/manga (`category=27&leisure_book_genre=comics%26mangas`), triée par nouveauté. Parsing de `__NEXT_DATA__` sans navigateur, empreinte TLS Chrome via `curl_cffi`, et sortie résidentielle gratuite : SOCKS5 en espace utilisateur sur le Raspberry Pi du domicile, joignable par tunnel SSH inverse (`ssh -R 127.0.0.1:1080:127.0.0.1:8080`) ; ni root, ni port entrant, ni fournisseur tiers. Cadence 10 min ±, collecteur désactivé par défaut.
- Conséquences : dédoublonnage inchangé `(source_name, external_id)` ; une annonce Leboncoin est nouvelle si `published_at > alert.created_at`, sans empreinte de premier scan ni migration ; le premier cycle réussi pose une référence silencieuse (aucune alerte rétroactive) ; une page entièrement plus récente que le cycle précédent déclenche la lecture bornée de la page 2 ; aucun frais Vinted n'est appliqué et aucun port n'est inventé (`estimated_total_cents` reste `NULL`, pas de ligne d'écart livré) ; la légende porte « Plateforme : Leboncoin ». Vinted — collecteur, parsing, poller, barème — reste inchangé. Un blocage DataDome met le collecteur en pause croissante et le signale sur Telegram ; un tunnel coupé suspend la collecte sans toucher aux autres sources.
- Limites : 35 annonces par page, soit une fenêtre de quelques dizaines de minutes à cette cadence ; une page saturée est rattrapée par une seconde page, jamais au-delà ; aucune collecte si le Pi ou le tunnel est indisponible ; `robots.txt` reste en contradiction avec cette décision, assumée par l'utilisateur et bornée à ce flux.

<a id="adr-053"></a>

## ADR-053 : Les seuils de mesure Leboncoin sont pré-enregistrés, sans oracle de rappel

Date : 2026-10-20 · Statut : **accepté**.

- Contexte : la collecte Leboncoin existe (ADR-052) mais aucune mesure ne distingue le transport de la valeur produit. L'utilisateur fixe la cible de latence à p95 ≤ 10 min, refuse un oracle externe (recherche sauvegardée par e-mail ou échantillon manuel), accepte un soak de 48 h avec réserve écrite, et demande la question de merge après l'étape 2 du plan de mesure.
- Décision et raison : pré-enregistrer les seuils avant la fin des mesures. Cadence 600 s ± jitter, justifiée par la latence (moyenne T/2 = 5 min, p95 ≈ 9,5 min) et par la marge de page (≈ 10 annonces par cycle pour 35 places). Latence mesurée depuis `published_at`, disponible pour Leboncoin seulement. Le soak de 48 h borne le taux de blocage à 1,04 % par cycle (95 %) en l'absence d'échec, avec la réserve que les blocages sont corrélés et que la borne binomiale est optimiste. Le rappel n'est pas mesuré faute d'oracle : aucune promesse de rappel ni d'exhaustivité n'est faite. Un canari de schéma quotidien doit alerter avant que le parsing ne se dégrade.
- Conséquences : l'activation peut revendiquer une latence mesurée, une couverture calculée et un taux de blocage borné, pas un rappel. Un oracle accepté plus tard rouvre la mesure du rappel. Les annonces épinglées sont exclues par la règle de fraîcheur, pas par un filtre dédié.
- Constat du 20 octobre : un challenge DataDome est survenu après 11 h et 65 cycles (10:58 UTC), suivi d'un second ; le collecteur applique son retrait progressif et le soak l'imite depuis 11:13 UTC. La borne de 1,04 % par cycle ne vaut que pour un soak sans échec, hypothèse désormais démentie ; le taux observé et la durée du blocage sont consignés dans [les mesures](../measurements/leboncoin-baseline.md).

<a id="adr-054"></a>

## ADR-054 : La clause anti-contournement est retirée d'`AGENTS.md` pour tout le dépôt

Date : 2026-10-20 · Statut : **accepté**.

- Contexte : `AGENTS.md` interdisait « Aucun contournement anti-bot ». Le collecteur Leboncoin (ADR-052) interroge un site protégé par DataDome dont le `robots.txt` interdit `/recherche`. L'utilisateur décide le retrait de cette clause, et le 22 octobre 2026 confirme qu'il s'agit d'un **changement global**, pas d'une exception limitée à ce collecteur.
- Décision et raison : la phrase est supprimée du paragraphe « Limites sensibles et qualité ». Aucune règle ne l'autorise désormais, et aucune source ne doit plus s'y référer comme à une interdiction en vigueur. Les décisions antérieures qui formulaient cette interdiction sont corrigées pour ne plus contredire l'état actuel des règles.
- Conséquences : **les autres interdictions demeurent inchangées et ne sont ni levées ni assouplies** — aucun paiement, aucune confirmation d'achat, aucun choix de relais automatisé, aucune automatisation d'interaction marketplace, aucun userscript, aucune extension, aucun script injecté. ADR-049 et ADR-047 conservent leurs règles propres et perdent seulement leur clause de non-contournement. ADR-052 garde son périmètre produit (un seul flux, une seule cadence) ; le retrait, lui, n'est plus borné à ce collecteur. Ce qui reste borné est l'usage opérationnel : sortie résidentielle, cadence de 10 minutes, aucun retry agressif, pause croissante sur refus, arrêt si le blocage dure.
