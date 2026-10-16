# Bouton « Acheter » — préparation du checkout Vinted, intégrée au bot

> **Statut (mesuré le 14/10/2026)** : le parcours est une **expérimentation
> clôturée en échec** dans les conditions testées — **ce constat n'est pas une
> validation**. La préparation du panier reçoit un **défi DataDome** y compris
> avec une session authentifiée ; dans **notre environnement testé**, la
> préparation serveur est **non opérationnelle** — sans que cela démontre une
> impossibilité générale. `VINTED_CHECKOUT_PREPARATION_ENABLED` reste `false` (par
> défaut) : le chemin décrit ci-dessous est inactif hors activation explicite.
> Voir la clôture au §9.
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
(vérifiée en base) ; verrou anti-double-clic (`SET NX`, rattaché à un jeton) ;
cache du lien préparé ; **coupe-circuit par cause, dès le premier échec** (30 min
après un blocage ou une limitation, 15 min après un délai ou une panne réseau,
5 min sinon) ; délais bornés ; session en clé Redis dédiée, jamais loguée ni
renvoyée ; aucune relance automatique après une opération incertaine.

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
| La création de checkout est coupée | coupe-circuit actif après un échec (§4) |

---

## 4. Coupe-circuit et reprise

La création est **coupée dès le premier échec**, pour la durée de sa cause :
**30 min** après un **défi anti-robot** (DataDome) ou une **limitation** (HTTP
429) ; **15 min** après un **délai dépassé** ou une **panne réseau** ; **5 min**
pour le reste (session refusée, article indisponible, vendeur introuvable,
réponse inattendue). Pendant la suspension, chaque clic répond « la création de
checkout est coupée » et propose l'annonce : **aucune relance automatique**, aucune
boucle de reconnexion. La reprise est **explicite** : laisser expirer la pause et
recliquer. La coupure est **globale** : un échec sur une annonce suspend aussi les
autres, comme le verrou.

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

**Vérifié en réel** :

- **session anonyme** (sans écriture possible) : les routes du parcours sont
  atteintes — transaction refusée (`auth_required`), panier refusé par un **défi
  DataDome** `blocked` ;
- **session authentifiée** (expérience du 14/10/2026, clics réels, sans point
  relais ni paiement) : le vendeur n'a pu être résolu sur des annonces anciennes
  (page rendue sans `seller_id`), et la création du panier a été refusée par
  **DataDome** (`403`, en-tête `x-dd-b`, corps captcha) mesuré avec la session ;
- le coupe-circuit suspend effectivement les préparations après un blocage
  (durée observée : 1800 s).

**Conclusion, bornée à notre environnement de test** : la réussite authentifiée
de bout en bout **n'a pas été atteinte** avec **notre** client et **notre**
serveur — le parcours y est **non opérationnel**. Ce n'est **ni une validation ni
une preuve d'impossibilité générale** : d'autres clients, dont des services
d'« autobuy » commerciaux, annoncent ce parcours, mais leurs promesses ne
constituent ni une preuve de fiabilité ni une autorisation de Vinted. Voir la
clôture au §9.

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

---

## 9. État des hypothèses et constats (audit du 14/10/2026, sans nouvelle requête)

### Hypothèses restantes, non vérifiées

1. **`seller_id` sur la page article.** Deux mesures contredisent l'hypothèse
   d'un repli fiable : page Mac (anonyme, article A) contenant `seller_id` ;
   page VPS (session, article B) rendant `200` **sans** `seller_id`. Les deux
   mesures varient par l'appareil, la session **et** l'article : la cause exacte
   est **non établie**. Le repli HTML n'est **pas réparé** : quand
   `seller_external_id` manque à l'ingestion **et** que la page ne le porte pas,
   la préparation s'arrête sur `SELLER_UNKNOWN` et pose une coupure **globale**
   de 5 min (un article sans identifiant vendeur suspend les autres).
2. **Une réponse `400 INVALID_INPUT` ne laisse aucun effet de bord.** C'est une
   hypothèse fondée sur la convention HTTP (un 400 rejette avant création), **non
   vérifiée** chez Vinted. Comme elle n'est pas établie, **aucun repli n'est
   tenté** sur ce code : un second essai pourrait créer une deuxième
   conversation. Le repli vers `/api/v2/conversations` ne subsiste que sur un
   `404` (route absente, donc rien n'a pu être créé), une seule fois, sans boucle.
3. **« Concurrence limitée », jamais « absence de doublon ».** Le verrou est
   global, atomique à l'acquisition (`SET NX`) et **rattaché à un jeton** : la
   libération vérifie que la clé porte encore notre jeton avant de la supprimer.
   Cette vérification n'est pas atomique (pas de `EVAL` Lua) : entre la lecture et
   la suppression, la clé peut expirer puis être reprise, et le `DEL` retirerait
   alors le verrou du nouveau détenteur — fenêtre résiduelle négligeable, car chaque
   libération est précédée d'une pause active et la coupure est **revérifiée dans**
   le verrou. Le verrou vit 45 s et couvre `2 × timeout` (vérifié par un test) ; il
   est **délibérément conservé après une réussite** jusqu'à son expiration — le lien
   mis en cache prend alors le relais **pour cette annonce** (une autre annonce reste
   refusée `BUSY` jusqu'à 45 s). Autres limites assumées : si le verrou expire
   pendant une opération en vol, un clic peut entrer ; et si Redis est muet, ni le
   verrou ni la coupure ne s'appliquent (garde-fou, pas dépendance dure). Les
   requêtes de création **ne portent aucun identifiant d'idempotence** : après un
   résultat incertain et l'expiration de la pause, un clic **explicite** peut créer
   une **seconde** conversation. C'est une limite connue, pas une garantie
   d'unicité ; aucun second essai automatique n'a lieu.

### Comportements couverts par les tests isolés

- tout échec coupe la création dès le premier essai (durée par cause) ;
- **concurrence limitée** : deux clics concurrents ne produisent, tant que le
  verrou est vivant, qu'une opération — ce n'est pas l'absence de doublon ;
- le verrou n'est libéré qu'après vérification de son jeton (un verrou repris
  survit, sur un statut que l'ancien code libérait) ;
- coupure posée avant la libération du verrou (ordre vérifié pas-à-pas) ;
- coupure revérifiée à l'intérieur du verrou ;
- `400 INVALID_INPUT` ne déclenche **aucun** repli ; `404` déclenche un repli
  unique, sans boucle ;
- drapeau coupé : ni session lue, ni base, ni Redis, ni Vinted — y compris depuis
  le bouton « Acheter » d'un ancien message ;
- journaux de refus sans URL ni valeur sensible.

### Statut de clôture

**Dans notre environnement testé** (notre client, notre serveur, session
authentifiée), le parcours serveur est un **échec** : DataDome a refusé la
construction du panier. Ce constat n'est **pas** une validation de la
fonctionnalité, et **pas** une preuve d'impossibilité technique générale : il est
borné à ce qui a été observé. `VINTED_CHECKOUT_PREPARATION_ENABLED` reste `false`,
le checkout serveur reste **non opérationnel**, et aucun contournement n'est
ajouté.
