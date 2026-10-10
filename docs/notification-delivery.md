# F03-B — accusé Telegram et reprise du marquage

## Contrat

Un résultat est `acknowledged` seulement avec un `aiogram.types.Message`, un
`message_id` strictement positif et le destinataire demandé. Il atteste un accusé
API exploitable, pas la lecture par l'utilisateur. `None`, un objet inexploitable
ou un reçu pour un autre destinataire restent `unknown`.

`rejected` est réservé à une erreur locale avant l'appel ou aux réponses négatives
traduites par `BaseSession.check_response` d'aiogram 3.31.0 en `TelegramBadRequest`,
`TelegramUnauthorizedError`, `TelegramForbiddenError` ou `TelegramNotFound`
(400/401/403/404). Les erreurs réseau, timeouts, décodage, 5xx et exceptions non
classifiées restent `unknown`. Cette classification repose sur le client verrouillé,
pas sur une expérimentation de Telegram réel.

Les deux essais photo, leur délai et le repli texte sont conservés. Un refus final
n'efface pas une tentative antérieure inconnue. Un accusé ultérieur autorise le
marquage, mais une photo précédemment acceptée sans réponse peut avoir produit un
doublon. Les doubles des tests simulent cette situation.

## Persistance et reprise

L'annonce et les matches pending sont commités avant l'envoi. Après l'accusé,
le marquage est commité pour ce seul destinataire. Une erreur SQL ordinaire
(SQLAlchemy, timeout ou I/O) déclenche le rollback protégé F03-A/F09, puis une
lecture fraîche : un marqueur déjà durable termine la reprise ; un match pending
permet **un seul** nouvel essai de marquage/commit, sans renvoyer Telegram. Un match
disparu n'est pas recréé et ne permet pas d'affirmer un marquage durable.

Le rollback expire les objets ORM : la reprise recharge l'annonce et les
alertes/livres encore présents, avec leurs relations, avant de servir les autres
destinataires. La protection locale de l'annonce reste tenue. L'annulation est
propagée après le nettoyage protégé ; elle ne déclenche pas une reprise SQL.
Le commit final reste inconditionnel. Son échec ne défait pas les marqueurs
précédemment commités.

Un second échec SQL ou une issue Telegram inconnue peut laisser un pending.
F01 conserve sa reprise lors d'une recollecte, avec risque de doublon explicitement
accepté. Aucune garantie « exactement une fois », aucune reprise d'une annonce qui
n'est plus collectée. Le reçu n'est conservé qu'en mémoire ; après redémarrage,
aucune information durable supplémentaire ne permet de départager ces cas.

## Diagnostics

`notification_delivery` corrèle `alert_id`, `listing_id`, `attempt_id`, transport,
numéro de tentative transport, résultat, `message_id` et étape de persistance.
La persistance indique séparément pending, inconnu ou notifié. L'exception n'est
exposée que par son nom de classe ; ni corps, payload, URL, secret, ni texte libre
d'exception ne sont ajoutés. `listing_ingested` atteste l'ingestion, pas la livraison.

Le drainage F09 attend le rollback protégé, y compris lorsqu'il est créé après
le début des annulations. Ce nettoyage reste coopératif : crash et arrêt forcé
ne sont pas rendus durables par ce lot.

## Vérification

Les tests rejouent le vrai cas d'usage et F01, avec doubles Telegram, événements
contrôlés et base temporaire. Sur PostgreSQL, le lecteur d'état et l'ingestion
utilisent des connexions distinctes (identifiants de backend vérifiés).
Les erreurs de commit avant/après application sont injectées localement ; aucune
panne réelle de Telegram, aucun crash et aucun arrêt forcé ne sont revendiqués.
Aucune migration ou réparation historique.
