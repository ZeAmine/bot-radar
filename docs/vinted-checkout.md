# Bouton « Acheter » — deux parcours, un seul paiement manuel

> **Statut (mesuré le 14/10/2026)**
>
> - **Parcours local assisté (défaut)** : c'est lui qui porte le bouton
>   `[ Acheter ]`. Son contrat est verrouillé par des tests, mais son exécution
>   réelle dans ton navigateur reste **à vérifier par toi** (§1.4).
> - **Parcours serveur** : livré mais **coupé par défaut**, **bloqué par DataDome
>   aujourd'hui** et **non validé de bout en bout** (§2).
>
> Dans les deux cas : le choix du point relais, le paiement et sa confirmation
> sont **entièrement manuels**, et **aucune garantie de non-bannissement n'est
> offerte** (§4).

---

## 1. Parcours local assisté (défaut)

### 1.1 Principe

1. Depuis une alerte, tu cliques `[ Acheter ]`. Telegram ouvre l'annonce avec un
   **marqueur d'intention** dans l'URL : `https://www.vinted.fr/items/<id>-…`
   suivi de `#botradar-acheter=<id>`.
2. Dans **ton navigateur**, le userscript
   [`scripts/vinted_checkout_assist.user.js`](../scripts/vinted_checkout_assist.user.js)
   reconnaît le marqueur, **vérifie que la page est bien l'article demandé**
   (comparaison des identifiants), retire le marqueur de l'historique, attend le
   bouton natif sous 10 secondes, et le déclenche **une seule fois**.
3. Vinted ouvre son parcours d'achat. **Tu choisis ton point relais et tu paies
   toi-même.** Le script ne fait plus rien ensuite.

Le marqueur vit dans le **fragment** de l'URL : il n'est jamais transmis à Vinted,
et il ne contient aucun secret. `[ Voir l'annonce ]` reste l'accès **sans
automatisation** : lien ordinaire, sans marqueur.

### 1.2 Ce que le script ne fait jamais

Périmètre verrouillé par `tests/unit/test_vinted_checkout_assist.py` et
`tests/js/checkout_assist_harness.mjs` :

- pas de point relais, pas de livraison, pas de « Payer », pas de confirmation ;
- pas de cookie, pas d'appel réseau, pas d'événement synthétique, pas de boucle ;
- **aucun clic sans marqueur**, ni sur une visite d'annonce ordinaire ;
- aucun clic si l'article n'est pas le bon, si le bouton est absent, ambigu,
  désactivé ou invisible, si la page a été redirigée (connexion) ou si un CAPTCHA
  / un blocage anti-robot apparaît ;
- aucun redéclenchement après retour arrière ou rechargement (marqueur consommé).

### 1.3 Installer le userscript

1. Installer Tampermonkey ou Violentmonkey dans ton navigateur habituel (tu l'as
   déjà pour d'autres scripts).
2. Créer un nouveau script et y coller le contenu de
   [`scripts/vinted_checkout_assist.user.js`](../scripts/vinted_checkout_assist.user.js).
3. Vérifier que les deux lignes `@match` sont bien limitées à
   `https://www.vinted.fr/items/*` et `https://vinted.fr/items/*`.

Aucune configuration côté bot : le lien marqué est inerte sans le script.

### 1.4 Où le lien s'ouvre — et la limite de ce parcours

| Contexte d'ouverture | Le userscript s'exécute ? | Résultat |
|---|---|---|
| **Navigateur habituel** (Safari/Chrome avec l'extension) | Oui | Déclenchement unique du bouton « Acheter », puis checkout manuel |
| **Navigateur intégré de Telegram** | **Non** | Le marqueur est ignoré ; la page reste une annonce ordinaire |
| **Application Vinted** | **Non** | Idem ; l'application ouvre l'article sans marqueur |

Pour ouvrir dans le navigateur habituel : **appui long** sur le bouton
« Acheter » (iOS) ou **menu ⋮ → Ouvrir dans le navigateur** (Android). Sans ce
geste, rien n'est automatisé — et c'est le repli normal.

**Procédure de vérification sur un article (à faire une fois)** :

1. Depuis une alerte réelle, appui long sur `[ Acheter ]` → « Ouvrir dans
   Safari/Chrome ».
2. La page doit être celle de l'article ; le marqueur disparaît de la barre
   d'adresse dès qu'il est traité, et l'interface Vinted passe d'elle-même à
   l'écran de choix de livraison (checkout).
3. **Arrête-toi là** : ne choisis pas le point relais, ne paie pas, ferme l'onglet.
4. Si rien ne se passe : vérifier que Tampermonkey est activé et que la page
   ouverte est bien `/items/<id>`. Le script ne clique **pas** si l'article est
   vendu, si une connexion est demandée ou si un CAPTCHA apparaît.

Tant que cette vérification n'a pas été faite, considère le parcours comme
**non validé en réel** : les tests automatisés prouvent la logique du script, pas
le rendu Vinted du jour.

**Vérifié en lecture seule le 14/10/2026 sur une annonce vivante** : le marqueur
survit à l'ouverture de la page, l'identifiant de la page correspond à celui du
marqueur, le sélecteur natif `[data-testid="item-buy-button"]` rend **exactement
un** bouton « Acheter » activé, et aucune protection anti-robot n'est présente.
**Non vérifié** : l'arrivée effective au checkout — elle demande ta session
connectée et un déclenchement réel, et s'arrête avant le choix du point relais.

### 1.5 Désactiver le parcours local

Désactiver ou supprimer le script dans Tampermonkey : `[ Acheter ]` redevient un
lien d'annonce ordinaire. Rien à couper côté bot.

---

## 2. Parcours serveur (coupé par défaut, non validé)

Quand `VINTED_CHECKOUT_PREPARATION_ENABLED=true`, `[ Acheter ]` devient un
**callback** : le bot tente de créer la transaction puis le panier avec ta
session de compte injectée, et répond `[ Poursuivre sur Vinted ]` avec le lien de
checkout. **Le flag est prioritaire** : pour revenir au parcours local, le
remettre à `false`.

### 2.1 Activer (si tu acceptes le risque et l'échec probable)

```dotenv
VINTED_CHECKOUT_PREPARATION_ENABLED=true
VINTED_CHECKOUT_TIMEOUT_SECONDS=12.0
VINTED_CHECKOUT_LINK_TTL_SECONDS=600
```

### 2.2 Capturer et injecter la session de compte

1. Se connecter à `https://www.vinted.fr` dans ton navigateur.
2. Outils de développement → onglet **Réseau** → n'importe quelle requête vers
   `www.vinted.fr` ou `api.vinted.fr`.
3. Copier la valeur de l'en-tête de requête **`cookie`** (elle inclut les cookies
   HttpOnly, contrairement à `document.cookie`).
4. La coller dans un fichier **hors dépôt**, par exemple
   `~/.vinted-checkout-cookies`, puis `chmod 600 ~/.vinted-checkout-cookies`.

```bash
python scripts/sync_vinted_checkout_session.py \
  --cookie-file ~/.vinted-checkout-cookies \
  --api-url http://127.0.0.1:8000 \
  --token "$INTERNAL_API_TOKEN"
```

Le script lit l'en-tête (ou un JSON équivalent), **valide la session sur le corps
de la réponse** (`code == 0` ; la passerelle rend `200` avec un corps de refus),
puis l'injecte. Ni mot de passe ni valeur de cookie n'est jamais affiché. Contrôle
d'état, sans valeur exposée :

```bash
curl -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"configured": true, "authenticated": true, "cookie_names": [...], "has_access_token": true}
```

La session expire avec les jetons Vinted (quelques heures à ~une journée).

### 2.3 Comportement en échec

| Message | Cause |
|---|---|
| Article plus disponible | vendu ou retiré |
| Session Vinted non configurée / plus valide | session jamais injectée, expirée ou refusée |
| Vérification anti-robot | DataDome a répondu : terminer manuellement depuis l'annonce |
| Vinted limite temporairement les demandes | réponse 429 |
| Vinted n'a pas répondu à temps | délai dépassé ; vérifier les messages Vinted avant de recliquer |
| Une préparation est déjà en cours | double clic : la première opération continue |
| Préparations suspendues | coupe-circuit actif après un blocage ou des échecs (§2.4) |

### 2.4 Coupe-circuit et reprise

Après un **défi anti-robot** (DataDome) ou une **limitation** (HTTP 429), les
préparations sont suspendues **30 minutes** ; après **trois échecs** doux (session
invalide, délai, panne réseau, réponse inattendue) en 15 minutes, elles le sont
15 minutes. Pendant la suspension, chaque clic répond « préparations suspendues »
et propose l'annonce : **aucune relance automatique**, aucune boucle de
reconnexion. La reprise est **explicite** : laisser expirer la pause et recliquer.
Aucun contournement de CAPTCHA, de rotation d'IP ou de compte n'existe.

### 2.5 Désactiver

`VINTED_CHECKOUT_PREPARATION_ENABLED=false` + redémarrage : `[ Acheter ]`
redevient le lien assisté du §1. Pour retirer la session stockée :

```bash
curl -X DELETE -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"status": "success"}
```

Rien n'est purgé automatiquement : la clé Redis expire d'elle-même en 18 h.

---

## 3. Mécanisme établi (pourquoi le parcours serveur n'est pas un simple lien)

Établi en lisant le client web de Vinted (scripts servis par `www.vinted.fr`),
puis vérifié par des requêtes bornées :

- **Aucun lien simple n'ouvre le checkout.** La page `/checkout` exige un
  `purchase_id` créé côté serveur. Le bouton « Acheter » du site enchaîne
  `POST /api/v2/conversations` (ou `api.vinted.fr/messaging/main/inquiries`),
  puis `POST /api/v2/purchases/checkout/build`, puis navigue vers
  `/checkout?purchase_id=…&order_id=…&order_type=transaction`.
- **La session doit être authentifiée** (un visiteur anonyme reçoit
  `user_login_required` / `Access denied`) ; le résultat appartient au **compte**,
  pas à la session — aucune session n'est transférée entre appareils.
- **DataDome protège le `checkout/build`** : une sonde serveur a reçu le défi
  `geo.captcha-delivery.com`. Le défi est traité comme un **signal**, pas comme un
  obstacle à franchir.
- **Effet de bord** : la préparation ouvre le fil de transaction
  acheteur↔vendeur que le bouton « Acheter » de Vinted ouvre lui-même, avant tout
  paiement. Le parcours local, lui, ouvre ce fil par le clic natif sur la page —
  comportement normal de l'interface.

---

## 4. Protections, limites et risques

**Protections logicielles** (contre les erreurs du code) : déclenchement après un
clic explicite uniquement ; marqueur d'intention vérifié article par article ;
un seul clic, jamais de rafale ; aucune action sans marqueur ; point relais et
paiement hors du périmètre ; côté serveur, verrou anti-double-clic, cache de lien,
coupe-circuit, délais bornés, aucune relance aveugle.

**Risques liés aux règles de Vinted** (hors de portée de ces protections) : les
conditions de Vinted **interdisent explicitement** l'usage d'outils logiciels
externes — « bots, programmes de scraping, programmes de crawling, spiders » — et
le *reverse engineering* du site. Un clic automatisé sur l'interface reste de
l'automatisation : **aucune fréquence ni proximité de l'interface ne prouve
l'absence de risque**, et Vinted peut restreindre ou suspendre un compte. La
responsabilité d'usage reste celle du titulaire.

Ce document ne recommande **aucune** infrastructure de contournement (proxy
dédié, rotation de comptes, résolution automatique de CAPTCHA) : ce serait
aggraver le risque, pas le réduire.
