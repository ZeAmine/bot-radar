#!/usr/bin/env python3
"""Invariance des services dépendants pendant un déploiement limité à l'app.

Un déploiement applicatif ne doit pas toucher PostgreSQL ni Redis. Cette
propriété a déjà été perdue une fois : le `up -d app` du 16 octobre 2026 a
recréé `radar_postgres`, parce que Compose réconcilie les dépendances d'un
service avant de démarrer celui-ci — et que la définition de `postgres` avait
changé, donc son empreinte de configuration aussi.

Le fait est établi, pas déduit. `docker compose config --hash postgres` calculé
sur la définition alors déployée rend `cd74a20e…`, qui est exactement
l'empreinte portée par le conteneur `radar_postgres` en service. Les deux
définitions résolvent pourtant le **même** environnement : ce qui les sépare est
la commande de leur `healthcheck`, et cette commande entre dans l'empreinte.

La parade est `--no-deps` sur le démarrage applicatif. Mais une parade qu'on ne
contrôle pas ne vaut pas mieux qu'une simulation : ce module capture l'identité
des dépendances avant l'arrêt de l'app et la relit après son démarrage. Un
service recréé ou redémarré change de `Id` ou de `StartedAt` — deux valeurs
qu'une recréation ne peut pas préserver.

Comparer `Running` ne suffirait pas : un conteneur redémarré est `Running` lui
aussi, et c'est précisément le cas qu'on cherche à voir. C'est pourquoi
`StartedAt` fait partie de l'identité au même titre que `Id`, et pourquoi les
trois issues — recréation, redémarrage, disparition — sont nommées séparément.
"""

from __future__ import annotations

from dataclasses import dataclass

# Les services dont l'invariance est exigée : le déploiement ne les nomme
# jamais, donc tout changement de leur part est un effet de bord.
DEPENDENT_SERVICES: tuple[str, ...] = ("postgres", "redis")

# Services qu'une fenêtre de maintenance a explicitement le droit de recréer.
# Ce n'est pas une tolérance générale : la liste est nominative, elle doit rester
# vide en temps normal, et `postgres` n'y figure jamais. Une recréation autorisée
# est **annoncée** par le contrôle, pas tue : l'opérateur doit savoir que son
# déploiement a recréé le service, même quand c'est attendu. Un redémarrage sans
# recréation reste un écart, même autorisé : c'est un comportement imprévu.
AUTHORIZED_RECREATION: tuple[str, ...] = ()

# Services qu'aucune autorisation ne peut couvrir. Une recréation y perd des
# données qu'une fenêtre de maintenance ne rend pas, donc la garantie ne doit pas
# dépendre du contenu d'une liste : elle est inscrite ici, et `compare` la fait
# primer sur `authorized_recreation`. Une liste mal remplie reste ainsi sans
# effet plutôt que dangereuse.
NEVER_RECREATED: tuple[str, ...] = ("postgres",)


class DependencyError(Exception):
    """Une dépendance n'a pas pu être inspectée : le déploiement doit s'arrêter."""


@dataclass(frozen=True)
class InvarianceVerdict:
    """Le résultat d'un contrôle d'invariance, en trois issues distinctes.

    `verifiee` et `ecarts` ne se confondent pas en un booléen : « je n'ai pas pu
    regarder » et « j'ai regardé, tout va bien » ne sont pas la même information,
    et les traiter pareil reviendrait à garantir sans preuve. `detail` porte la
    raison de la non-vérification, pour que l'opérateur sache quoi corriger.
    """

    verifiee: bool
    ecarts: tuple[str, ...] = ()
    detail: str = ""
    autorisees: tuple[str, ...] = ()

    @classmethod
    def concluante(
        cls,
        ecarts: list[str],
        autorisees: list[str] | None = None,
    ) -> InvarianceVerdict:
        """L'inspection a abouti : les écarts constatés font foi.

        `autorisees` porte les recréations qu'une fenêtre de maintenance a
        demandées. Elles ne bloquent pas le déploiement, mais elles restent
        visibles : une recréation attendue qui n'est pas annoncée redeviendrait
        un changement silencieux.
        """
        return cls(
            verifiee=True,
            ecarts=tuple(ecarts),
            autorisees=tuple(autorisees or ()),
        )

    @classmethod
    def non_verifiee(cls, detail: str) -> InvarianceVerdict:
        """L'inspection n'a pas abouti : ni intact, ni modifié n'est établi."""
        return cls(verifiee=False, detail=detail)


@dataclass(frozen=True)
class ServiceIdentity:
    """L'identité d'un conteneur, telle qu'elle permet de détecter un changement.

    `container_id` et `started_at` sont les deux valeurs qu'une recréation ne
    peut pas préserver. `running` est conservé séparément : il dit si le service
    était opérationnel, ce qui est une autre question que son invariance.
    """

    service: str
    container_id: str
    started_at: str
    running: bool

    @property
    def is_operational(self) -> bool:
        """Un service arrêté ne peut pas servir l'app : le déployer serait vain."""
        return self.running and bool(self.container_id)


def capture_script(remote_dir: str, services: tuple[str, ...] = DEPENDENT_SERVICES) -> str:
    """Le script distant qui relève l'identité des dépendances.

    Le relevé n'appelle que `docker` : aucun interpréteur supplémentaire n'est
    supposé présent sur l'hôte, et le parcours reste vérifiable avec un seul
    double. Chaque service rend une ligne `service<TAB>id|started|running`, ou
    `service<TAB>` quand son conteneur n'existe pas — « absent » est exactement
    ce qu'un contrôle préalable doit pouvoir refuser.
    """
    noms = " ".join(services)
    return f"""set -eu
cd {remote_dir}
for svc in {noms}; do
  cid=$(docker compose ps -a -q "$svc" 2>/dev/null || true)
  if [ -z "$cid" ]; then
    printf '%s\\t\\n' "$svc"
  else
    docker inspect --format '{{{{.Id}}}}|{{{{.State.StartedAt}}}}|{{{{.State.Running}}}}' "$cid" \\
      | sed "s|^|$svc\\t|"
  fi
done
"""


def decode_capture(payload: str) -> dict[str, ServiceIdentity | None]:
    """Relit le relevé, en identités par service.

    Le relevé est une suite de lignes `service<TAB>id|started|running`, ou
    `service<TAB>` pour un conteneur absent. Il est lu comme une donnée, jamais
    évalué. Une sortie vide est refusée explicitement plutôt que traduite en un
    dictionnaire vide, qui se lirait comme « aucun changement » alors qu'il
    signifie « je n'ai rien pu lire ».
    """
    if not payload.strip():
        raise DependencyError("relevé vide : les dépendances n'ont pas été inspectées")

    identites: dict[str, ServiceIdentity | None] = {}
    for ligne in payload.splitlines():
        if not ligne.strip():
            continue
        service, _, reste = ligne.partition("\t")
        service = service.strip()
        if not service:
            raise DependencyError("relevé illisible : service vide")
        if not reste.strip():
            identites[service] = None
            continue
        parts = reste.strip().split("|")
        if len(parts) != 3:
            raise DependencyError(
                f"relevé illisible pour {service} : {len(parts)} champ(s) au lieu de 3"
            )
        cid, started, running = parts
        identites[service] = ServiceIdentity(
            service=service,
            container_id=cid,
            started_at=started,
            running=running.strip().lower() == "true",
        )
    if not identites:
        raise DependencyError("relevé illisible : aucun service décrit")
    return identites


def assert_operational(
    before: dict[str, ServiceIdentity | None],
    services: tuple[str, ...] = DEPENDENT_SERVICES,
) -> list[str]:
    """Vérifie que les dépendances sont là et tournent **avant** d'arrêter l'app.

    Contrôler leur invariance après coup ne sert à rien si l'app a été arrêtée
    alors qu'une dépendance manquait : le service serait resté indisponible pour
    rien. Ce contrôle est donc préalable, et il porte sur tous les services
    requis, pas seulement sur ceux qui répondent.
    """
    problemes: list[str] = []
    for service in services:
        identite = before.get(service)
        if identite is None:
            problemes.append(f"{service} : conteneur introuvable")
        elif not identite.is_operational:
            problemes.append(f"{service} : conteneur arrêté ({identite.container_id[:12]})")
    return problemes


def compare(
    before: dict[str, ServiceIdentity | None],
    after: dict[str, ServiceIdentity | None],
    services: tuple[str, ...] = DEPENDENT_SERVICES,
    authorized_recreation: tuple[str, ...] = AUTHORIZED_RECREATION,
) -> tuple[list[str], list[str]]:
    """Rend les écarts constatés entre deux relevés, séparés en deux listes.

    Trois cas sont distingués parce qu'ils n'appellent pas la même lecture : un
    identifiant différent est une **recréation**, un identifiant identique avec
    un `StartedAt` différent est un **redémarrage**, et une disparition est une
    **suppression**. Les confondre en un seul « a changé » priverait l'opérateur
    de l'information qui lui dit quoi regarder.

    La première liste bloque le déploiement. La seconde porte les recréations
    qu'une fenêtre de maintenance a demandées : elles ne bloquent pas, mais elles
    sont rendues pour être annoncées. Seule la recréation est couverte par
    l'autorisation — un redémarrage, une disparition ou une apparition du même
    service restent des écarts, donc bloquants.
    """
    ecarts: list[str] = []
    autorisees: list[str] = []
    for service in services:
        avant = before.get(service)
        apres = after.get(service)

        if avant is None and apres is None:
            continue
        if apres is None:
            ecarts.append(f"{service} : conteneur disparu après le démarrage de l'app")
            continue
        if avant is None:
            ecarts.append(f"{service} : conteneur apparu, inexistant avant le déploiement")
            continue
        if avant.container_id != apres.container_id:
            if service in authorized_recreation and service not in NEVER_RECREATED:
                autorisees.append(
                    f"{service} : conteneur RECRÉÉ par la fenêtre de maintenance "
                    f"({avant.container_id[:12]} → {apres.container_id[:12]})"
                )
            else:
                ecarts.append(
                    f"{service} : conteneur RECRÉÉ "
                    f"({avant.container_id[:12]} → {apres.container_id[:12]})"
                )
            continue
        if avant.started_at != apres.started_at:
            ecarts.append(
                f"{service} : conteneur REDÉMARRÉ "
                f"({avant.started_at} → {apres.started_at}), identifiant inchangé"
            )
            continue
        if avant.running != apres.running:
            ecarts.append(
                f"{service} : état d'exécution modifié ({avant.running} → {apres.running})"
            )
    return ecarts, autorisees
