"""Le déploiement ne touche pas les dépendances, et le prouve.

Le 16 octobre 2026, `make deploy` a recréé `radar_postgres` alors qu'il ne visait
que `app` : Compose réconcilie les dépendances d'un service avant de le démarrer,
et l'empreinte de configuration de `postgres` avait changé. La parade est
`--no-deps`, mais une parade n'est une garantie que si son effet est constaté.

Ces tests portent donc sur le **comportement** : la commande réellement construite
et la décision réellement prise face à des identités qui changent. Une recherche
textuelle dans le script ne dirait rien de ce qui est exécuté.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from deploy import (  # noqa: E402
    APP_ONLY_UP,
    ROLLBACK_TAG_PREFIX,
    Context,
    step_rebuild,
    step_start,
    step_stop,
)
from deploy_dependencies import (  # noqa: E402
    DEPENDENT_SERVICES,
    DependencyError,
    ServiceIdentity,
    assert_operational,
    capture_script,
    compare,
    decode_capture,
)

POSTGRES_ID = "179105aa2890d53b2c258d0512450d7a8261fa131658f509ccfc1320851b997e"
REDIS_ID = "ca2d0d871cb6c2f2f5d716458269ba29cd2cddb7bd2d4364adbd071cb07d5ff1"
POSTGRES_STARTED = "2026-10-16T01:02:48.053688530Z"
REDIS_STARTED = "2026-09-25T17:38:32.055624606Z"


def _identite(service: str, cid: str, started: str, running: bool = True) -> ServiceIdentity:
    return ServiceIdentity(service=service, container_id=cid, started_at=started, running=running)


def _releve(postgres=None, redis=None) -> dict:
    """Le relevé tel que le VPS le rendrait, en identités déjà décodées."""
    return {"postgres": postgres, "redis": redis}


def _releve_texte(postgres=None, redis=None) -> str:
    """Le même relevé, sérialisé comme le script distant le produit.

    Une ligne par service : `service<TAB>id|started|running`, ou `service<TAB>`
    quand le conteneur n'existe pas.
    """

    def _ligne(service, identite):
        if identite is None:
            return f"{service}\t"
        return (
            f"{service}\t{identite.container_id}|{identite.started_at}"
            f"|{'true' if identite.running else 'false'}"
        )

    return f"{_ligne('postgres', postgres)}\n{_ligne('redis', redis)}\n"


class _RemoteIdentites:
    """Rend un relevé JSON différent à chaque appel, dans l'ordre fourni.

    Les étapes du déploiement relèvent l'identité deux fois — avant l'arrêt,
    après le démarrage. Un faux qui rendrait toujours la même chose ne pourrait
    pas distinguer « rien n'a changé » de « je n'ai rien regardé ».
    """

    def __init__(
        self,
        releves: list[str],
        codes: list[int] | None = None,
        demarrage_code: int = 0,
    ) -> None:
        self.releves = list(releves)
        self.codes = list(codes) if codes else [0] * len(releves)
        self.demarrage_code = demarrage_code
        self.scripts: list[str] = []

    def run(self, script: str) -> tuple[int, str, str]:
        self.scripts.append(script)
        index = len(self.scripts) - 1
        if ".State.StartedAt" in script:
            if index < len(self.releves):
                return self.codes[index], self.releves[index], ""
            return 0, self.releves[-1], ""
        # Le script de démarrage : son code porte la santé de l'app.
        return self.demarrage_code, "ok", ""


def _ctx(**overrides) -> Context:
    defaults = {
        "host": "ubuntu@example",
        "key": "/tmp/id_test",
        "remote_dir": "~/bot-radar",
        "repo_root": Path("/tmp/depot"),
    }
    defaults.update(overrides)
    return Context(**defaults)  # type: ignore[arg-type]


INCHANGE_POSTGRES = _identite("postgres", POSTGRES_ID, POSTGRES_STARTED)
INCHANGE_REDIS = _identite("redis", REDIS_ID, REDIS_STARTED)


def test_the_start_command_excludes_dependencies():
    """`--no-deps` est présent : c'est la parade au mécanisme observé.

    Sans lui, Compose réconcilie les dépendances avant de démarrer `app` et
    recrée tout service dont l'empreinte de configuration a changé.
    """
    assert "--no-deps" in APP_ONLY_UP


def test_the_start_command_targets_only_app():
    """La seule cible est `app` : aucun autre service n'est nommé.

    Nommer `postgres` ou `redis` — même sans les recréer — les soumettrait à la
    réconciliation de Compose, ce qui est exactement ce qu'on veut éviter.
    """
    services = [mot for mot in APP_ONLY_UP.split() if not mot.startswith("-")]
    # « docker compose up » précède les options ; les cibles sont en fin de ligne.
    cibles = [mot for mot in services if mot in {"app", "postgres", "redis"}]
    assert cibles == ["app"]


def test_the_start_command_never_pulls_or_rebuilds_the_image():
    """L'image servie est celle qui vient d'être construite et vérifiée."""
    assert "--no-build" in APP_ONLY_UP
    assert "--pull never" in APP_ONLY_UP


def test_step_start_actually_sends_the_guarded_command():
    """La commande envoyée au VPS est bien celle qui porte la garde.

    Vérifier la constante ne prouverait rien si l'étape construisait sa propre
    ligne : c'est le script réellement transmis qui doit la contenir.
    """
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 0

    demarrage = [s for s in remote.scripts if "compose up" in s]
    assert len(demarrage) == 1
    assert "--no-deps" in demarrage[0]
    assert "--no-deps app" in demarrage[0]


def test_unchanged_dependencies_are_accepted():
    """Mêmes identifiants et mêmes démarrages : aucun écart."""
    avant = _releve(INCHANGE_POSTGRES, INCHANGE_REDIS)
    apres = _releve(INCHANGE_POSTGRES, INCHANGE_REDIS)

    assert compare(avant, apres) == ([], [])


def test_step_start_succeeds_when_dependencies_are_unchanged():
    """Le déploiement est déclaré réussi, et le dit."""
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 0


def test_an_unverifiable_inspection_fails_even_with_a_healthy_app(capsys):
    """App saine, mais inspection impossible : le déploiement n'est pas validé.

    C'est le cas qui doit échouer « fermé ». Ne pas avoir pu relire l'état des
    dépendances ne prouve pas qu'elles sont intactes : conclure au succès
    reviendrait à garantir sans preuve.
    """
    remote = _RemoteIdentites([""], codes=[1], demarrage_code=0)
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1

    sortie = capsys.readouterr().out
    assert "NON VÉRIFIÉE" in sortie
    # Ni modifiées ni intactes : l'incertitude est dite, pas comblée.
    assert "Ni modifiées ni intactes" in sortie
    assert "dépendances inchangées" not in sortie


def test_an_unreadable_inspection_fails_even_with_a_healthy_app(capsys):
    """Un relevé illisible est traité comme une inspection ratée, pas un succès."""
    remote = _RemoteIdentites(["postgres\tcid-sans-les-trois-champs"])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1
    assert "NON VÉRIFIÉE" in capsys.readouterr().out


def test_an_unknown_before_state_cannot_be_declared_intact(capsys):
    """Sans relevé d'avant, il n'y a rien à comparer : le succès est refusé.

    Ce cas ne doit pas se produire dans le parcours nominal — `step_stop`
    capture toujours avant — mais s'il survient, il ne doit pas se lire comme
    une invariance prouvée : le déploiement échoue sans annoncer un succès.
    """
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote, dependencies_before=None)

    assert step_start(ctx) == 1

    sortie = capsys.readouterr().out
    assert "NON VÉRIFIÉE" in sortie
    # Aucun succès n'est annoncé, et rien n'est réparé.
    assert "dépendances inchangées" not in sortie
    assert all("restart" not in s and "rollback" not in s for s in remote.scripts)


def test_an_unverifiable_inspection_never_triggers_a_repair():
    """Aucune réparation n'est tentée sur la seule base d'une inspection ratée.

    Les seules commandes envoyées sont le démarrage et le relevé : ni
    redémarrage, ni `up` de dépendance, ni retour arrière.
    """
    remote = _RemoteIdentites([""], codes=[1])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1

    for script in remote.scripts:
        assert "restart" not in script
        assert "compose stop" not in script
        # Aucun service dépendant n'est nommé comme cible d'un `up`.
        assert "up -d" not in script or "--no-deps app" in script


def test_the_invariance_check_runs_even_when_the_app_is_unhealthy():
    """Santé en échec : le contrôle d'invariance a quand même lieu.

    Un déploiement qui a touché PostgreSQL doit le dire, même si l'app n'est
    pas saine — c'est même le moment où l'information compte le plus.
    """
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)], demarrage_code=1)
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1

    assert len([s for s in remote.scripts if ".State.StartedAt" in s]) == 1


def test_a_drift_is_reported_even_when_the_app_is_unhealthy(capsys):
    """App non saine **et** dépendance recréée : l'écart est nommé.

    Les deux problèmes sont distincts, et l'écart ne doit pas être absorbé par
    l'échec de santé.
    """
    recree = _identite("postgres", "d" * 64, POSTGRES_STARTED)
    remote = _RemoteIdentites([_releve_texte(recree, INCHANGE_REDIS)], demarrage_code=1)
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1
    assert "RECRÉÉ" in capsys.readouterr().out


def test_a_recreated_dependency_is_a_failure():
    """Un identifiant différent est une recréation : le déploiement échoue.

    C'est le cas exact du 16 octobre 2026 : le conteneur a été remplacé, et sa
    recréation ne doit jamais passer pour un succès.
    """
    recree = _identite("postgres", "a" * 64, "2026-10-16T09:00:00.000000000Z")
    ecarts, _ = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS), _releve(recree, INCHANGE_REDIS)
    )

    assert len(ecarts) == 1
    assert "RECRÉÉ" in ecarts[0]
    assert "postgres" in ecarts[0]


def test_step_start_fails_when_a_dependency_was_recreated():
    """L'étape rend un code d'échec, et n'annonce pas un déploiement réussi."""
    recree = _identite("postgres", "b" * 64, POSTGRES_STARTED)
    remote = _RemoteIdentites([_releve_texte(recree, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1


def test_a_restarted_dependency_is_a_failure_even_with_the_same_id():
    """Même identifiant, `StartedAt` différent : le conteneur a redémarré.

    C'est ce cas qui rend `StartedAt` nécessaire : comparer l'état `Running`
    seul le laisserait passer, puisque le conteneur redémarré tourne aussi.
    """
    redemarre = _identite("redis", REDIS_ID, "2026-10-16T09:00:00.000000000Z")
    ecarts, _ = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS), _releve(INCHANGE_POSTGRES, redemarre)
    )

    assert len(ecarts) == 1
    assert "REDÉMARRÉ" in ecarts[0]
    # Le message nomme les deux instants : un redémarrage n'est pas une recréation.
    assert REDIS_STARTED in ecarts[0]
    assert "identifiant inchangé" in ecarts[0]


def test_a_restarted_dependency_fails_the_start_step():
    """Le redémarrage seul suffit à refuser le succès."""
    redemarre = _identite("redis", REDIS_ID, "2026-10-16T09:00:00.000000000Z")
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, redemarre)])
    ctx = _ctx(remote=remote, dependencies_before=_releve(INCHANGE_POSTGRES, INCHANGE_REDIS))

    assert step_start(ctx) == 1


def test_a_disappeared_dependency_is_reported_as_such():
    """Un service disparu est nommé « disparu », pas confondu avec un autre écart."""
    ecarts, _ = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS), _releve(INCHANGE_POSTGRES, None)
    )

    assert len(ecarts) == 1
    assert "disparu" in ecarts[0]


def test_an_execution_state_change_is_reported():
    """Un conteneur arrêté pendant le déploiement est un écart, même à id constant."""
    arrete = _identite("redis", REDIS_ID, REDIS_STARTED, running=False)
    ecarts, _ = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS), _releve(INCHANGE_POSTGRES, arrete)
    )

    assert len(ecarts) == 1
    assert "état d'exécution" in ecarts[0]


def test_both_dependencies_can_drift_at_once():
    """Deux écarts sont rendus ensemble : l'opérateur voit tout, pas le premier."""
    recree = _identite("postgres", "c" * 64, POSTGRES_STARTED)
    redemarre = _identite("redis", REDIS_ID, "2026-10-16T09:00:00.000000000Z")
    ecarts, _ = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS), _releve(recree, redemarre)
    )

    assert len(ecarts) == 2


def test_a_missing_dependency_stops_before_the_app_is_stopped():
    """Une dépendance absente arrête tout **avant** l'interruption de l'app.

    Arrêter l'app pour découvrir ensuite qu'une dépendance manque ne ferait que
    rendre le service indisponible sans rien permettre de réparer.
    """
    remote = _RemoteIdentites([_releve_texte(None, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 1

    arrets = [s for s in remote.scripts if "compose stop" in s]
    assert arrets == [], "l'app ne doit pas être arrêtée si une dépendance manque"


def test_a_stopped_dependency_stops_before_the_app_is_stopped():
    """Une dépendance arrêtée est refusée de la même façon qu'une absente."""
    arrete = _identite("postgres", POSTGRES_ID, POSTGRES_STARTED, running=False)
    remote = _RemoteIdentites([_releve_texte(arrete, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 1
    assert [s for s in remote.scripts if "compose stop" in s] == []


def test_an_impossible_inspection_stops_before_the_app_is_stopped():
    """Une inspection qui échoue n'est pas un succès : rien n'est supposé.

    Ne pas pouvoir lire l'état des dépendances ne dit pas qu'elles vont bien ;
    le déploiement s'arrête donc avant d'interrompre quoi que ce soit.
    """
    remote = _RemoteIdentites([""], codes=[1])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 1
    assert [s for s in remote.scripts if "compose stop" in s] == []


def test_an_unreadable_payload_stops_before_the_app_is_stopped():
    """Un relevé illisible est refusé, jamais traduit en « aucun changement »."""
    remote = _RemoteIdentites(["ceci n'est pas du JSON"])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 1
    assert [s for s in remote.scripts if "compose stop" in s] == []


def test_operational_dependencies_let_the_stop_proceed():
    """Le cas nominal : les deux dépendances répondent, l'app est arrêtée."""
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 0
    assert len([s for s in remote.scripts if "compose stop" in s]) == 1
    # L'identité relevée est conservée pour la comparaison d'après démarrage.
    assert ctx.dependencies_before is not None
    assert ctx.dependencies_before["postgres"] == INCHANGE_POSTGRES


def test_operational_reports_every_missing_service():
    """Tous les manquants sont nommés, pas seulement le premier rencontré."""
    problemes = assert_operational(_releve(None, None), DEPENDENT_SERVICES)

    assert len(problemes) == 2
    assert any("postgres" in p for p in problemes)
    assert any("redis" in p for p in problemes)


def test_the_capture_payload_round_trips():
    """Le relevé produit par le script distant est relu sans perte."""
    payload = _releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)
    relu = decode_capture(payload)

    assert relu["postgres"] == INCHANGE_POSTGRES
    assert relu["redis"] == INCHANGE_REDIS


def test_a_missing_container_is_read_as_absent_not_skipped():
    """`null` devient « absent », et non une clé oubliée.

    Une clé oubliée se lirait comme « rien à signaler » : c'est le contraire de
    ce qu'un conteneur manquant doit produire.
    """
    relu = decode_capture(_releve_texte(None, INCHANGE_REDIS))

    assert "postgres" in relu
    assert relu["postgres"] is None


def test_an_empty_payload_is_refused():
    """Un relevé vide est une inspection ratée, pas une absence de changement."""
    with pytest.raises(DependencyError):
        decode_capture("")


def test_a_payload_with_a_malformed_line_is_refused():
    """Une ligne sans ses trois champs est refusée, jamais devinée.

    Une identité tronquée lue comme valide ferait passer un conteneur changé
    pour un conteneur stable : l'erreur doit remonter, pas être absorbée.
    """
    with pytest.raises(DependencyError):
        decode_capture("postgres\tabc|2026-01-01T00:00:00Z")


def test_a_payload_describing_nothing_is_refused():
    """Un relevé sans aucun service ne prouve aucune invariance."""
    with pytest.raises(DependencyError):
        decode_capture("\n  \n")


def test_the_capture_script_inspects_every_required_service():
    """Le script relevé couvre tous les services dont l'invariance est exigée."""
    script = capture_script("/tmp/bot-radar")

    for service in DEPENDENT_SERVICES:
        assert service in script
    # Les trois champs de l'identité sont demandés à Docker, pas deux sur trois.
    for champ in (".Id", ".State.StartedAt", ".State.Running"):
        assert champ in script


def test_the_stop_step_preserves_the_running_image_before_any_build():
    """L'image du conteneur est taguée avant l'arrêt, donc avant le build.

    `docker compose build app` réécrit `latest` en place : sans cette copie,
    l'image réellement en service devient inatteignable et le retour arrière
    n'a plus de référence à restaurer.
    """
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 0

    arret = next(s for s in remote.scripts if "compose stop app" in s)
    assert "docker inspect --format '{{.Image}}'" in arret
    assert "docker tag" in arret
    assert ROLLBACK_TAG_PREFIX in arret
    # Le tag est créé avant l'arrêt : l'image du conteneur en service est capturée.
    assert arret.index("docker tag") < arret.index("docker compose stop app")


def test_the_stop_step_verifies_the_tag_points_to_the_captured_image():
    """Le tag est vérifié, pas seulement créé.

    Un tag qui ne pointerait pas sur l'image capturée restaurerait autre chose
    que ce qui tournait : la vérification ferme cette possibilité. Le script
    compare l'identifiant du tag à celui du conteneur et échoue sinon.
    """
    remote = _RemoteIdentites([_releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS)])
    ctx = _ctx(remote=remote)

    assert step_stop(ctx) == 0

    arret = next(s for s in remote.scripts if "compose stop app" in s)
    assert "tagged=$(docker inspect --format" in arret
    assert '[ "$tagged" = "$image_id" ]' in arret
    assert "tag de retour incohérent" in arret


def test_the_rollback_tag_is_recorded_in_the_context():
    """Le tag conservé est retenu, pour être revérifié après le build."""

    class _RemoteTag(_RemoteIdentites):
        def run(self, script):
            self.scripts.append(script)
            if ".State.StartedAt" in script:
                return 0, _releve_texte(INCHANGE_POSTGRES, INCHANGE_REDIS), ""
            if "docker tag" in script:
                return (
                    0,
                    "image de retour conservée : bot-radar-app:rollback-20260101-120000 "
                    "(sha256:abc)\n"
                    "RADAR_ROLLBACK_TAG=bot-radar-app:rollback-20260101-120000\n"
                    "arrêt app confirmé avant synchronisation\n",
                    "",
                )
            return 0, "ok", ""

    ctx = _ctx(remote=_RemoteTag([]))
    assert step_stop(ctx) == 0
    assert ctx.rollback_image == "bot-radar-app:rollback-20260101-120000"


def test_the_build_rechecks_the_rollback_tag_still_exists():
    """Après le build, le tag de retour est revérifié.

    C'est le moment où `latest` a été réécrit : si le tag de retour a disparu,
    il faut le savoir maintenant et non au moment du retour arrière.
    """
    remote = _RemoteIdentites([])
    ctx = _ctx(remote=remote, rollback_image="bot-radar-app:rollback-20260101-120000")

    assert step_rebuild(ctx) == 0

    build = next(s for s in remote.scripts if "compose build app" in s)
    assert "docker inspect" in build
    assert "bot-radar-app:rollback-20260101-120000" in build
    assert build.index("compose build app") < build.index("docker inspect")
    assert "tag de retour disparu" in build


def test_the_build_still_builds_when_no_rollback_tag_is_known():
    """Sans tag connu, le build a lieu : la revérification est conditionnelle.

    Le parcours nominal renseigne toujours le tag ; ce cas couvre une reprise
    d'étape isolée, où l'absence de tag ne doit pas bloquer la construction.
    """
    remote = _RemoteIdentites([])
    ctx = _ctx(remote=remote)

    assert step_rebuild(ctx) == 0

    build = next(s for s in remote.scripts if "compose build app" in s)
    assert "docker inspect" not in build


def test_authorized_recreation_of_redis_is_reported_but_not_blocking():
    """Une fenêtre de maintenance peut recréer Redis : ce n'est pas un écart.

    La recréation reste annoncée. La rendre silencieuse reviendrait à ne plus
    savoir qu'un déploiement a touché une dépendance — précisément ce que ce
    contrôle existe pour empêcher.
    """
    recree = _identite("redis", "d" * 64, "2026-10-23T10:00:00.000000000Z")
    ecarts, autorisees = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS),
        _releve(INCHANGE_POSTGRES, recree),
        authorized_recreation=("redis",),
    )

    assert ecarts == []
    assert len(autorisees) == 1
    assert "redis" in autorisees[0]


def test_authorization_never_covers_postgres():
    """`postgres` reste protégé même si une autorisation le nommait par erreur.

    Une recréation de PostgreSQL perd des données qu'aucune fenêtre de
    maintenance ne rend ; le contrôle ne doit pas pouvoir être neutralisé par
    une liste d'autorisation mal remplie.
    """
    recree = _identite("postgres", "c" * 64, POSTGRES_STARTED)
    ecarts, autorisees = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS),
        _releve(recree, INCHANGE_REDIS),
        authorized_recreation=("postgres",),
    )

    assert len(ecarts) == 1
    assert "postgres" in ecarts[0]
    assert autorisees == []


def test_authorized_recreation_still_flags_a_restart():
    """Autoriser une recréation n'autorise pas un redémarrage inexpliqué.

    Un conteneur redémarré sans recréation est un comportement imprévu : la
    configuration n'a pas changé, donc rien n'explique le redémarrage.
    """
    redemarre = _identite("redis", REDIS_ID, "2026-10-23T10:00:00.000000000Z")
    ecarts, autorisees = compare(
        _releve(INCHANGE_POSTGRES, INCHANGE_REDIS),
        _releve(INCHANGE_POSTGRES, redemarre),
        authorized_recreation=("redis",),
    )

    assert len(ecarts) == 1
    assert "REDÉMARRÉ" in ecarts[0]
    assert autorisees == []
