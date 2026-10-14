# Bouton « Acheter » — préparation du checkout Vinted, intégrée au bot

> **Statut (mesuré le 14/10/2026)** : le chemin serveur atteint les vraies routes
> et classe correctement les refus, mais la préparation du panier reçoit un
> **défi DataDome** et n'a **pas** été validée avec une session authentifiée. Le
> parcours **n'est pas opérationnel** tant que le checkout du bon article n'a pas
> été effectivement ouvert dans ta session, sur ton appareil (§6).
>
> **Aucun userscript, aucune extension navigateur, aucun script injecté dans la
> page** : cette méthode est abandonnée (ADR-049) et ne fait pas partie du
> produit.

Depuis une alerte Telegram, le bouton `[ Acheter ]` (affiché seulement si
`VINTED_CHECKOUT_PREPARATION_ENABLED`) prépare **à la demande** le panier Vinted
de l'annonce avec une session de compte injectée, puis propose
`[ Poursuivre sur Vinted ]`. L'utilisateur ouvre ce lien dans **sa propre**
session, choisit son point relais et confirme lui-même le paiement.

---

## 1. Principe et protections

1. Le clic Telegram déclenche un **callback** ; rien n'est préparé à l'avance, et
   aucun appel réseau n'existe sur le chemin d'envoi de l'alerte (invariant n°1).
2. Le bot vérifie l'utilisateur autorisé et l'association annonce ↔ alerte, puis
   crée une transaction acheteur↔vendeur et un panier via les **routes du client
   web**, avec ta session.
3. Il répond avec un lien de checkout ; **le choix du point relais, le paiement
   et sa confirmation restent entièrement manuels**.
4. En cas d'échec, `[ Voir l'annonce ]` reste l'accès fonctionnel.

Protections conservées : allowlist Telegram et association annonce ↔ alerte
(vérifiée en base) ; verrou anti-double-clic atomique (`SET NX`) ; cache du lien
préparé ; **coupe-circuit** (30 min après blocage ou limitation, 15 min après
trois échecs doux) ; délais bornés ; session en clé Redis dédiée, jamais loguée
ni renvoyée ; aucune relance automatique après une opération incertaine.

**Ce que la fonctionnalité ne fait pas** : elle ne paie pas, ne choisit pas le
point relais, ne confirme pas l'achat, et ne contourne aucune protection de
Vinted (pas de résolution de CAPTCHA, pas de rotation d'IP ou de compte, pas de
camouflage).

---

## 2. Activer

### 2.1 Lever l'interrupteur

```dotenv
VINTED_CHECKOUT_PREPARATION_ENABLED=true
VINTED_CHECKOUT_TIMEOUT_SECONDS=12.0
VINTED_CHECKOUT_LINK_TTL_SECONDS=600
```

Coupé (défaut), le bouton `[ Acheter ]` n'apparaît pas et aucune session de
compte n'est nécessaire.

### 2.2 Capturer la session de compte

1. Se connecter à `https://www.vinted.fr` dans son navigateur.
2. Outils de développement → onglet **Réseau** → n'importe quelle requête vers
   `www.vinted.fr` ou `api.vinted.fr`.
3. Copier la valeur de l'en-tête de requête **`cookie`** (elle inclut les cookies
   HttpOnly, contrairement à `document.cookie`).
4. La coller dans un fichier **hors dépôt**, par exemple
   `~/.vinted-checkout-cookies`, puis `chmod 600 ~/.vinted-checkout-cookies`.

### 2.3 Valider et injecter

```bash
python scripts/sync_vinted_checkout_session.py \
  --cookie-file ~/.vinted-checkout-cookies \
  --api-url http://127.0.0.1:8000 \
  --token "$INTERNAL_API_TOKEN"
```

Le script lit l'en-tête (ou un JSON équivalent), **valide la session sur le corps
de la réponse** (`code == 0` ; la passerelle rend `200` avec un corps de refus),
puis l'injecte. Ni mot de passe ni valeur de cookie n'est jamais affiché.
Contrôle d'état, sans valeur exposée :

```bash
curl -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"configured": true, "authenticated": true, "cookie_names": [...], "has_access_token": true}
```

La session expire avec les jetons Vinted (quelques heures à ~une journée).

---

## 3. Comportement en échec

| Message | Cause |
|---|---|
| Article plus disponible | vendu ou retiré |
| Session Vinted non configurée / plus valide | session jamais injectée, expirée ou refusée |
| Vérification anti-robot | DataDome a répondu : terminer manuellement depuis l'annonce |
| Vinted limite temporairement les demandes | réponse 429 |
| Vinted n'a pas répondu à temps | délai dépassé ; vérifier les messages Vinted avant de recliquer |
| Une préparation est déjà en cours | double clic : la première opération continue |
| Préparations suspendues | coupe-circuit actif après un blocage ou des échecs (§4) |

---

## 4. Coupe-circuit et reprise

Après un **défi anti-robot** (DataDome) ou une **limitation** (HTTP 429), les
préparations sont suspendues **30 minutes** ; après **trois échecs** doux (session
invalide, délai, panne réseau, réponse inattendue) en 15 minutes, elles le sont
15 minutes. Pendant la suspension, chaque clic répond « préparations suspendues »
et propose l'annonce : **aucune relance automatique**, aucune boucle de
reconnexion. La reprise est **explicite** : laisser expirer la pause et recliquer.

Résoudre un blocage est un travail **manuel dans le navigateur** (ouvrir
l'annonce, terminer l'achat soi-même, éventuellement repasser par une
vérification humaine).

---

## 5. Désactiver

`VINTED_CHECKOUT_PREPARATION_ENABLED=false` + redémarrage : le bouton disparaît
des nouvelles alertes. Pour retirer la session stockée :

```bash
curl -X DELETE -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  https://radar.aminezegmou.fr/api/v1/internal/vinted/checkout-session
# {"status": "success"}
```

Rien n'est purgé automatiquement : la clé Redis expire d'elle-même en 18 h.

---

## 6. Statut réel et validation sur un article

**Vérifié en réel (session anonyme, sans écriture possible)** :

- les routes du parcours sont atteintes : transaction refusée à un visiteur
  anonyme (`auth_required`), panier refusé par un **défi DataDome** `blocked` ;
- le coupe-circuit suspend effectivement les préparations après un blocage
  (durée observée : 1800 s) ;
- la session d'injection est validée sur le corps de la réponse, pas sur le seul
  statut HTTP.

**Non vérifié** : la réussite authentifiée de bout en bout — donc **le parcours
n'est pas opérationnel** en l'état.

**Procédure minimale de validation (à faire une fois, sur un seul article)** :

1. capturer et injecter la session (§2.2–2.3), lever l'interrupteur ;
2. cliquer `[ Acheter ]` sur **une** alerte ciblée ;
3. si le bot répond `[ Poursuivre sur Vinted ]`, **ouvrir le lien dans ta
   session** et vérifier que le checkout affiche **le bon article** ;
4. **s'arrêter avant le choix du point relais et avant tout paiement**, puis
   refermer l'onglet ;
5. si le bot répond « Vérification anti-robot » ou « Préparations suspendues »,
   c'est que DataDome a refusé côté serveur : le parcours ne passe pas sans
   contourner la protection, et aucun contournement ne sera ajouté. Le repli est
   `[ Voir l'annonce ]`, à la main.

**Ce qui manque pour terminer cette vérification** : une session locale injectée
(les empreintes sont attendues dans `~/.vinted-checkout-cookies`, jamais dans le
dépôt) et une instance du bot joignable. Sans elles, seuls les refus anonymes
sont mesurables.

---

## 7. Mécanisme établi (pourquoi ce n'est pas un simple lien)

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
  paiement.

---

## 8. Risques

Les conditions de Vinted **interdisent explicitement** l'usage d'outils
logiciels externes — « bots, programmes de scraping, programmes de crawling,
spiders » — et le *reverse engineering* du site. Ce parcours utilise un client
automatisé avec ta session : **aucune fréquence ni proximité de l'interface ne
prouve l'absence de risque**, et Vinted peut restreindre ou suspendre un compte.
Les protections décrites ici sont contre les **erreurs du logiciel** (mauvais
article, double clic, session exposée, relance aveugle), pas contre les règles de
Vinted. La responsabilité d'usage reste celle du titulaire du compte.

Ce document ne recommande **aucune** infrastructure de contournement (proxy
dédié, rotation de comptes, résolution automatique de CAPTCHA, userscript ou
extension) : ce serait aggraver le risque, pas le réduire.
