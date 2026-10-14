# Bouton « Acheter » — préparation du checkout Vinted

> **Statut (mesuré le 14/10/2026)** : le chemin serveur atteint les vraies routes et
> classe correctement les refus, mais la préparation du panier reçoit
> **aujourd'hui un défi DataDome** et n'a **pas** été validée de bout en bout avec
> une session de compte. Le paiement, la confirmation et le point relais restent
> manuels, et aucune garantie de non-bannissement n'est offerte. Lire §1 et §5
> avant d'activer.

Depuis une alerte Telegram, le bouton `[ Acheter ]` prépare **à la demande** le
panier Vinted de l'annonce, puis propose `[ Poursuivre sur Vinted ]`. L'utilisateur
ouvre ce lien dans son propre navigateur ou l'application Vinted, choisit son point
relais et confirme lui-même le paiement.

> **Ce que la fonctionnalité ne fait pas** : elle ne paie pas, ne choisit pas le
> point relais, ne confirme pas l'achat et ne contourne aucune protection de Vinted.
> Elle ne prépare rien tant que le bouton n'est pas cliqué, et rien à l'envoi de
> l'alerte (invariant n°1 : aucune requête réseau ajoutée avant Telegram).

---

## 1. Mécanisme réellement établi

Le mécanisme a été établi en lisant le client web de Vinted (les scripts servis par
`www.vinted.fr`), puis vérifié par des requêtes de lecture seule :

- **Aucun lien simple n'ouvre le checkout.** La page `/checkout` exige un
  `purchase_id` créé côté serveur. Le bouton « Acheter » du site enchaîne :
  1. `POST www.vinted.fr/api/v2/conversations` (ou, selon le backend de messagerie
     du compte, `POST api.vinted.fr/messaging/main/inquiries`) — crée une
     transaction acheteur↔vendeur ;
  2. `POST www.vinted.fr/api/v2/purchases/checkout/build` — transforme cette
     transaction en panier et rend `checkout.id` ;
  3. navigation vers
     `/checkout?purchase_id=<panier>&order_id=<transaction>&order_type=transaction`.
- **La session doit être authentifiée.** Un visiteur anonyme reçoit
  `user_login_required` / `Access denied`. Le bot ne peut donc rien préparer sans
  une session de compte injectée.
- **Le résultat appartient au compte, pas à la session.** Le bot crée le panier
  avec la session injectée ; l'utilisateur ouvre ensuite le lien avec **sa propre**
  session. Aucune session n'est transférée entre appareils.
- **DataDome protège le `checkout/build`.** Une sonde serveur anonyme a reçu le défi
  `geo.captcha-delivery.com`. Aucun contournement n'est tenté : un défi se traduit
  par un message explicite et le repli `[ Voir l’annonce ]`. Une session contenant
  un cookie `datadome` valide peut passer ; rien ne le garantit.

### Effets de bord assumés (côté Vinted)

- La préparation **ouvre un fil de discussion avec le vendeur** (c'est le flux
  « Acheter » de Vinted lui-même) avant tout paiement.
- Elle crée une transaction et un panier côté compte. Rien n'est payé, l'article
  n'est pas acheté par le bot.
- La préparation n'est tentée qu'une fois par clic : pas de relance automatique en
  cas de délai dépassé (le résultat peut exister côté Vinted). Un cache court et un
  verrou anti-double-clic l'évitent aussi.

### Conditions d'utilisation de Vinted — ce qu'il faut savoir

Les conditions de Vinted **interdisent explicitement** l'usage d'outils logiciels
externes — « bots, programmes de scraping, programmes de crawling, spiders » — lors
de l'utilisation du site, sauf usage autorisé par Vinted, ainsi que le *reverse
engineering* et le *screen scraping* de tout ou partie du site. La préparation
automatisée du checkout tombe dans cette catégorie : elle est faite avec ta session
de compte et des appels réseau qui ne viennent pas de l'interface.

Conséquence honnête : **aucune fréquence faible ni aucun parcours « proche de
l'interface » ne garantit l'absence de risque.** Le coupe-circuit et les délais
bornés réduisent l'exposition, ils ne l'annulent pas. Contractuellement, Vinted
peut suspendre ou restreindre un compte pour ces pratiques, et la responsabilité
d'usage reste celle du titulaire du compte. Le fait que ce soit ton compte, avec ton
accord, ne change pas ce constat.

### La préparation serveur n'est pas viable aujourd'hui

Constat mesuré (14/10/2026), même avec un cookie de session structuré en en-tête
comme le fait le client web :

- la **transaction** (`POST /api/v2/conversations`) est refusée à une session
  anonyme (`user_login_required`) ;
- le **panier** (`POST /api/v2/purchases/checkout/build`) reçoit un **défi DataDome**
  (`geo.captcha-delivery.com`) servi dès l'appel serveur, sans authentification.

Aucun contournement n'est implémenté, et l'on n'en cherche pas : le défi est un
**signal, pas un obstacle à franchir**. Le chemin serveur reste livré pour l'article
ciblé, mais il n'a **pas** été validé de bout en bout dans ce lot (aucune session de
compte n'a été fournie). Voir §5 pour l'alternative locale.

---

## 2. Activer

### 2.1 Lever l'interrupteur

Dans le `.env` du bot (local et/ou production) :

```dotenv
VINTED_CHECKOUT_PREPARATION_ENABLED=true
VINTED_CHECKOUT_TIMEOUT_SECONDS=12.0
VINTED_CHECKOUT_LINK_TTL_SECONDS=600
```

Tant que l'interrupteur est `false`, le bouton `[ Acheter ]` n'apparaît pas et
aucune donnée de session de compte n'est nécessaire.

### 2.2 Capturer la session de compte

1. Se connecter à `https://www.vinted.fr` dans son navigateur.
2. Outils de développement → onglet **Réseau** → n'importe quelle requête vers
   `www.vinted.fr` ou `api.vinted.fr`.
3. Copier la valeur de l'en-tête de requête **`cookie`** (elle inclut les cookies
   HttpOnly, contrairement à `document.cookie`).
4. La coller dans un fichier **hors dépôt**, par exemple `~/.vinted-checkout-cookies`,
   puis `chmod 600 ~/.vinted-checkout-cookies`.

### 2.3 Valider et injecter

```bash
python scripts/sync_vinted_checkout_session.py \
  --cookie-file ~/.vinted-checkout-cookies \
  --api-url http://127.0.0.1:8000 \
  --token "$INTERNAL_API_TOKEN"
```

Le script :

- lit l'en-tête (ou un JSON équivalent) ;
- **valide la session auprès de Vinted** (`GET /api/v2/users/current`) avant toute
  injection — une session refusée n'est jamais installée ;
- l'injecte via `POST /api/v1/internal/vinted/checkout-session`, qui stocke les
  cookies dans Redis (clé `radar:vinted_checkout_session`, TTL 18 h) ;
- n'affiche **jamais** la valeur d'un cookie ni d'un jeton (seulement des noms et
  des compteurs).

Contrôle d'état (sans valeur exposée) :

```bash
curl -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"configured": true, "authenticated": true, "cookie_names": [...], "has_access_token": true}
```

La session expire avec les jetons Vinted (ordre de grandeur : quelques heures à
~une journée). Le bot ne stocke ni mot de passe ni identifiant : seulement les
cookies capturés, dans Redis, séparés de la session anonyme du scraper.

---

## 3. Utiliser

1. Recevoir une alerte Telegram et cliquer `[ Acheter ]`.
2. Le bot prépare le panier et répond avec `[ Poursuivre sur Vinted ]`.
3. Choisir son point relais et confirmer le paiement **dans Vinted**.
4. `[ Voir l’annonce ]` reste disponible à chaque étape, et sert de repli en cas
   d'échec.

Messages possibles en cas d'échec (le lien de l'annonce reste joint) :

| Message | Cause |
|---|---|
| Article plus disponible | vendu ou retiré |
| Session Vinted non configurée / plus valide | session jamais injectée, expirée ou refusée |
| Vérification anti-robot | DataDome a répondu : terminer manuellement depuis l'annonce |
| Vinted limite temporairement les demandes | réponse 429 |
| Vinted n'a pas répondu à temps | délai dépassé ; vérifier les messages Vinted avant de recliquer |
| Une préparation est déjà en cours | double clic : la première opération continue |
| Préparations suspendues | coupe-circuit actif après un blocage ou des échecs (§4 bis) |

## 4. Désactiver

Revenir à `VINTED_CHECKOUT_PREPARATION_ENABLED=false` puis redémarrer le
processus : le bouton disparaît des nouvelles alertes et les clics sur d'anciens
messages répondent « préparation désactivée ». Pour retirer aussi la session
stockée, sur geste explicite :

```bash
curl -X DELETE -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"status": "success"}
```

Rien n'est purgé automatiquement : la clé Redis expire d'elle-même en 18 h, et une
réinjection remplace l'ancienne session.

## 4 bis. Coupe-circuit et reprise

Après un **défi anti-robot** (DataDome) ou une **limitation** (HTTP 429), les
préparations sont suspendues **30 minutes** ; après **trois échecs** doux (session
invalide, délai, panne réseau, réponse inattendue) dans une fenêtre de 15 minutes,
elles le sont 15 minutes. Pendant la suspension, chaque clic répond « préparations
suspendues » et propose l'annonce : **aucune relance automatique n'a lieu**, aucune
boucle de reconnexion ne tourne.

La reprise est **explicite** : il suffit de laisser expirer la pause et de recliquer
sur « Acheter ». Il n'existe aucune route ni commande pour forcer la reprise, et
aucune pour purger la pause — le geste reste la fin naturelle du délai.

Résoudre un blocage est un travail **manuel dans le navigateur** (ouvrir l'annonce,
terminer l'achat soit-même, éventuellement repasser par une vérification humaine).
Le bot n'automatise ni CAPTCHA, ni rotation d'IP, ni rotation de compte.

---

## 5. Limites assumées et alternative locale

- **Aucune garantie contre une restriction de compte.** Les appels sont bornés,
  uniques, déclenchés par l'utilisateur et suspendus après un signal défavorable,
  mais l'usage d'un client automatisé reste contraire aux conditions de Vinted et
  sous la responsabilité de l'utilisateur.
- **Le chemin serveur est aujourd'hui bloqué par DataDome** (voir §1) : il n'a pas
  été validé de bout en bout, et rien ne garantit qu'une session de compte
  injectée suffira.
- **La session expire** et doit être recapturée périodiquement.
- **Un délai dépassé laisse un doute** : le message le dit et invite à vérifier
  Vinted avant de réessayer ; aucune relance automatique.

### Alternative locale, proportionnée

Le dépôt contient déjà un **userscript navigateur**
([`scripts/vinted_autobuy.user.js`](vinted_autobuy.user.js)) qui agit **dans ton
navigateur et ta session habituelle** : il clique le bouton « Acheter » de la page
article et sélectionne un point relais. Il ne se substitue pas à toi pour le
paiement — le bouton « Payer » est mis en évidence, c'est toi qui cliques.

Pourquoi c'est proportionné, et pas contourné : l'action naît de **ta** session et
de **ton** navigateur, sans transfert de cookie ni appel issu d'un serveur. C'est le
chemin le plus proche de l'interface possible. Cela **ne supprime pas** le risque :
rester sur le plan contractuel, l'automatisation de l'interface reste de
l'automatisation. Le userscript est un outil manuel, à utiliser consciemment.

Ce document ne recommande **aucune** infrastructure de contournement (proxy dédié,
rotation de comptes, résolution automatique de CAPTCHA) : ce serait aggraver le
risque, pas le réduire.
