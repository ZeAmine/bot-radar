# Bouton « Acheter » — préparation du checkout Vinted

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

---

## 5. Limites assumées

- **Aucune garantie contre une restriction de compte.** Les appels
  d'authentification au compte sont bornés, uniques et déclenchés par l'utilisateur,
  mais l'usage d'un client automatisé reste sous la responsabilité de l'utilisateur.
- **Le défi DataDome peut bloquer la préparation.** Aucun contournement n'est
  implémenté ; le repli est manuel.
- **La session expire** et doit être recapturée périodiquement.
- **Un délai dépassé laisse un doute** : le message le dit et invite à vérifier
  Vinted avant de réessayer ; aucune relance automatique.
- **La validation réelle de bout en bout n'est pas incluse dans cette livraison** :
  elle demande une session de compte injectée et ne doit comporter aucun paiement.
  Les tests automatisés remplacent la frontière Vinted (transport simulé) et ne
  déclenchent aucune opération réelle.
