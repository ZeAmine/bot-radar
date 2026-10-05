#!/usr/bin/env python3
"""Assainit les mots-clés dégénérés des alertes déjà enregistrées.

Le moteur IA a pu écrire des listes que rien ne bornait : une variante par suffixe de
lettre (« … 2a » … « … 2z ») sur un titre à numéro, une exclusion numérique nue
(« 1 ») qui rejette toute annonce contenant un chiffre. Le nettoyeur de
`app.domain.rules.keyword_hygiene` corrige la source ; ce script en applique le verdict
aux alertes **déjà stockées**, sans migration et sans toucher au code du bot.

Par défaut il ne fait que rapporter : c'est un `--dry-run` implicite. Rien n'est écrit
sans qu'un diff ait été lu — `--apply` seul se refuse et le dit ; `--apply --diff` ou
`--apply --yes` écrivent.

Le nettoyage est **idempotent** : une seconde exécution ne trouve plus rien à changer,
parce que le nettoyeur est déjà sans effet sur une liste assainie.

Le titre de chaque alerte sert de référence au nettoyeur : c'est lui qui décide qu'une
variante répète le titre ou qu'un suffixe s'ajoute à son numéro. Ce jugement dépend donc
du titre, et `--only-degenerate` permet de s'en passer : ce mode ne retire que ce qui est
dégénéré **par lui-même** — chiffre nu, caractère seul, code d'ouvrage, suffixe
d'énumération — sans jamais consulter le titre. Rien n'y est tronqué ni dédupliqué : un
rejeu aveugle en production ne peut pas alors emporter un terme qualifié, ce qui est
précisément ce qui est arrivé sur les exclusions de « Deep It ».

Usage :
    python scripts/clean_alert_keywords.py                    # rapport
    python scripts/clean_alert_keywords.py --diff             # rapport détaillé, motivé
    python scripts/clean_alert_keywords.py --diff --apply     # réécrit
    python scripts/clean_alert_keywords.py --apply --yes      # réécrit sans relire le diff
    python scripts/clean_alert_keywords.py --only-degenerate --diff
    python scripts/clean_alert_keywords.py --json --limit 20
    python scripts/clean_alert_keywords.py --include-inactive

En production :
    docker compose exec -T app python scripts/clean_alert_keywords.py --diff
    docker compose exec -T app python scripts/clean_alert_keywords.py --diff --apply

Codes de sortie : 0 rien à nettoyer (ou nettoyage appliqué), 1 des alertes sont à
nettoyer en mode rapport, 2 erreur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.session import AsyncSessionLocal
from app.domain.rules.keyword_hygiene import (
    SanitizedValues,
    TermRemoval,
    assess_exclusions,
    assess_variants,
)

EXIT_CLEAN, EXIT_CHANGES, EXIT_ERROR = 0, 1, 2

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

FAMILY_VARIANTS = "variantes"
FAMILY_EXCLUSIONS = "exclusions"

@dataclass(frozen=True)
class AlertKeywordPlan:
    """Ce que le nettoyeur retirerait d'une alerte, avant et après.

    Les deux listes sont conservées entières — et pas seulement le retrait — parce que
    c'est la comparaison `avant`/`après` qui rend le plan vérifiable : une liste
    assainie est identique à elle-même au second passage.
    """

    alert_id: str
    name: str
    variants_before: tuple[str, ...]
    variants_after: tuple[str, ...]
    exclusions_before: tuple[str, ...]
    exclusions_after: tuple[str, ...]
    inactive: bool = False
    variant_removals: tuple[TermRemoval, ...] = ()
    exclusion_removals: tuple[TermRemoval, ...] = ()
    only_degenerate: bool = False

    @property
    def variants_removed(self) -> list[str]:
        return [v for v in self.variants_before if v not in self.variants_after]

    @property
    def exclusions_removed(self) -> list[str]:
        return [e for e in self.exclusions_before if e not in self.exclusions_after]

    @property
    def changed(self) -> bool:
        """Le nettoyeur a-t-il quelque chose à retirer sur cette alerte ?"""
        return (
            self.variants_before != self.variants_after
            or self.exclusions_before != self.exclusions_after
        )


def _plan_family(
    before: tuple[str, ...],
    title: str,
    *,
    assess: Callable[[list[str] | None, str], SanitizedValues],
    only_degenerate: bool,
) -> tuple[tuple[str, ...], tuple[TermRemoval, ...]]:
    """Verdict d'une famille, tel que la règle l'a motivé.

    Aucun filtre ici : ce module ne décide rien, il relaie. La raison affichée vient
    du même enchaînement qui a écarté le terme, donc elle ne peut pas en diverger —
    c'était tout le risque d'un motif rejoué à côté des filtres.
    """
    outcome = assess(list(before), title)
    # La règle dit quelles positions elle a gardées : aucune recherche par valeur,
    # donc deux termes identiques ne font pas disparaître le survivant.
    after = tuple(before[index] for index in outcome.kept_indexes)
    if not only_degenerate:
        return after, outcome.removals

    # Portée restreinte : on repart de la liste d'origine et on n'en retire que ce qui
    # est dégénéré par lui-même. Tout le reste est conservé tel quel — y compris ce que
    # le titre, la déduplication ou le plafond auraient écarté — pour qu'un rejeu
    # aveugle ne puisse pas emporter un terme qualifié.
    dropped = {
        removal.index for removal in outcome.removals if removal.reason.is_degenerate
    }
    return (
        tuple(raw for index, raw in enumerate(before) if index not in dropped),
        tuple(removal for removal in outcome.removals if removal.reason.is_degenerate),
    )


def plan_cleanup(alert: Alert, *, only_degenerate: bool = False) -> AlertKeywordPlan:
    """Verdict du nettoyeur pour une alerte. Fonction pure : ni base, ni réseau."""
    variants = tuple(alert.variants or ())
    exclusions = tuple(alert.exclude_keywords or ())
    variants_after, variant_removals = _plan_family(
        variants,
        alert.name,
        assess=assess_variants,
        only_degenerate=only_degenerate,
    )
    exclusions_after, exclusion_removals = _plan_family(
        exclusions,
        alert.name,
        assess=assess_exclusions,
        only_degenerate=only_degenerate,
    )
    return AlertKeywordPlan(
        alert_id=str(alert.id),
        name=alert.name,
        variants_before=variants,
        variants_after=variants_after,
        exclusions_before=exclusions,
        exclusions_after=exclusions_after,
        inactive=alert.status != AlertStatus.ACTIVE,
        variant_removals=variant_removals,
        exclusion_removals=exclusion_removals,
        only_degenerate=only_degenerate,
    )


def apply_plan(alert: Alert, plan: AlertKeywordPlan) -> None:
    """Applique le verdict à l'objet en mémoire. L'appelant décide du commit."""
    alert.variants = list(plan.variants_after)
    alert.exclude_keywords = list(plan.exclusions_after)


async def read_alerts(
    *,
    include_inactive: bool = False,
    limit: int | None = None,
) -> list[Alert]:
    """Toutes les alertes, actives seules par défaut, dans l'ordre de création."""
    stmt = select(Alert).order_by(Alert.created_at)
    if not include_inactive:
        stmt = stmt.where(Alert.status == AlertStatus.ACTIVE)
    if limit is not None:
        stmt = stmt.limit(limit)

    async with AsyncSessionLocal() as session:
        return list((await session.execute(stmt)).scalars().all())


async def persist(plans: list[AlertKeywordPlan]) -> int:
    """Réécrit en base les seules alertes changées et rend le nombre de mises à jour.

    Le plan a été calculé sur une lecture antérieure ; on relit l'alerte avant d'écrire
    pour ne pas ressusciter une ligne supprimée entre-temps, et pour appliquer le verdict
    au document réel plutôt qu'à une copie détachée.
    """
    changed = [plan for plan in plans if plan.changed]
    if not changed:
        return 0

    written = 0
    async with AsyncSessionLocal() as session:
        for plan in changed:
            alert = await session.get(Alert, uuid.UUID(plan.alert_id))
            if alert is None:
                continue
            apply_plan(alert, plan)
            written += 1
        await session.commit()
    return written


def format_instant(moment: datetime) -> str:
    """Un instant lisible et sans ambiguïté de fuseau."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def render(
    plans: list[AlertKeywordPlan],
    *,
    apply: bool,
    cleaned_le: datetime,
    diff: bool = False,
) -> str:
    """Rapport lisible : en-tête daté, un bloc par alerte changée, un verdict final.

    En mode `diff`, chaque alerte montre ses listes avant → après et le motif de chaque
    retrait. Un rapport qui ne dit pas *pourquoi* se subit au lieu de se relire, et
    c'est ce qui manquait pour oser écrire.
    """
    inactives = sum(1 for plan in plans if plan.inactive)
    mode = "écriture" if apply else "rapport (aucune écriture)"
    scope = (
        "uniquement les termes dégénérés (titre jamais consulté)"
        if any(plan.only_degenerate for plan in plans)
        else "tous les verdicts du nettoyeur"
    )
    lines = [
        f"Nettoyage des mots-clés dégénérés — {format_instant(cleaned_le)}",
        f"Population lue : {len(plans)} alerte(s) (dont {inactives} inactive(s))",
        f"Mode : {mode}",
        f"Portée : {scope}",
        "",
    ]

    changed = [plan for plan in plans if plan.changed]
    for plan in changed:
        head = f"{DIM}{plan.alert_id[:8]}{RESET} {plan.name}"
        if diff:
            lines.append(head)
            for family, before, after, reasons in (
                (
                    FAMILY_VARIANTS,
                    plan.variants_before,
                    plan.variants_after,
                    plan.variant_removals,
                ),
                (
                    FAMILY_EXCLUSIONS,
                    plan.exclusions_before,
                    plan.exclusions_after,
                    plan.exclusion_removals,
                ),
            ):
                if not reasons:
                    continue
                lines.append(f"    {family} : {len(before)} → {len(after)}")
                lines.append(f"      avant : {', '.join(before) if before else '(aucune)'}")
                lines.append(f"      après : {', '.join(after) if after else '(aucune)'}")
                for removal in reasons:
                    lines.append(
                        f"      {RED}-{removal.raw}{RESET}  "
                        f"{DIM}{removal.reason.value}{RESET}"
                    )
            continue
        if plan.variants_removed:
            lines.append(
                f"  {RED}VARIANTS{RESET}   {head} "
                f"{DIM}— {len(plan.variants_before)} → {len(plan.variants_after)} : "
                f"{', '.join(plan.variants_removed)}{RESET}"
            )
        if plan.exclusions_removed:
            lines.append(
                f"  {RED}EXCLUSIONS{RESET} {head} "
                f"{DIM}— {len(plan.exclusions_before)} → {len(plan.exclusions_after)} : "
                f"{', '.join(plan.exclusions_removed)}{RESET}"
            )

    lines.append("")
    if not changed:
        lines.append(f"{GREEN}Aucun mot-clé dégénéré : rien à nettoyer.{RESET}")
    elif apply:
        lines.append(f"{GREEN}{len(changed)} alerte(s) nettoyée(s).{RESET}")
    else:
        lines.append(
            f"{RED}{len(changed)} alerte(s) à nettoyer — relire --diff, puis --apply.{RESET}"
        )
    return "\n".join(lines)


def _motifs(removals: tuple[TermRemoval, ...]) -> list[dict[str, Any]]:
    """Les retraits d'une famille, avec le motif que la règle leur a donné."""
    return [
        {
            "terme": removal.raw,
            "raison": removal.reason.value,
            "degenere": removal.reason.is_degenerate,
        }
        for removal in removals
    ]


def as_json(
    plans: list[AlertKeywordPlan],
    *,
    apply: bool,
    cleaned_le: datetime,
) -> str:
    """La même information sans mise en forme, en-tête compris."""
    return json.dumps(
        {
            "genere_le": cleaned_le.astimezone(UTC).isoformat(),
            "mode": "ecriture" if apply else "rapport",
            "portee": (
                "degenere"
                if any(plan.only_degenerate for plan in plans)
                else "complete"
            ),
            "population": {
                "lues": len(plans),
                "inactives": sum(1 for plan in plans if plan.inactive),
            },
            "alertes": [
                {
                    "alert_id": plan.alert_id,
                    "name": plan.name,
                    "variants": {
                        "avant": list(plan.variants_before),
                        "apres": list(plan.variants_after),
                        "retirees": plan.variants_removed,
                    },
                    "exclusions": {
                        "avant": list(plan.exclusions_before),
                        "apres": list(plan.exclusions_after),
                        "retirees": plan.exclusions_removed,
                    },
                    "motifs": {
                        "variantes": _motifs(plan.variant_removals),
                        "exclusions": _motifs(plan.exclusion_removals),
                    },
                }
                for plan in plans
                if plan.changed
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Applique le nettoyeur de mots-clés aux alertes déjà enregistrées : "
            "variantes issues d'une énumération et exclusions numériques nues."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="réécrit les mots-clés en base (exige --diff, sauf --yes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="rapport seul, explicite (c'est déjà le défaut)",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="détaille chaque alerte : avant → après, et le motif de chaque retrait",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="écrit sans exiger la relecture du diff (--apply seul refuse)",
    )
    parser.add_argument(
        "--only-degenerate",
        action="store_true",
        help=(
            "ne retire que ce qui est dégénéré par lui-même (chiffre nu, caractère "
            "seul, code d'ouvrage, suffixe d'énumération), sans consulter le titre : "
            "rien n'y est dédupliqué ni tronqué"
        ),
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--limit", type=int, help="nombre maximal d'alertes lues")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="inclure les alertes en pause ou supprimées",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """Le parcours complet du CLI : lire, planifier, rapporter, écrire si demandé."""
    if args.apply and args.dry_run:
        print(f"{RED}--apply et --dry-run s'excluent.{RESET}", file=sys.stderr)
        return EXIT_ERROR

    # On n'écrit pas ce qu'on n'a pas lu. Un diff non motivé se subit ; il faut qu'il
    # ait été affiché, ou que l'appelant prenne explicitement la responsabilité de
    # passer outre — c'est ce que dit `--yes`, et rien d'autre ne le dispense.
    if args.apply and not args.diff and not args.yes:
        print(
            f"{RED}Refus d'écrire : le diff n'a pas été lu.{RESET}\n"
            f"  Relancer avec --diff --apply pour voir ce qui part, "
            f"ou --apply --yes si vous savez ce que vous demandez.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        alerts = await read_alerts(
            include_inactive=args.include_inactive,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"{RED}Lecture des alertes impossible : {exc}{RESET}", file=sys.stderr)
        return EXIT_ERROR

    # L'instant où la population a été lue : c'est elle que le rapport décrit.
    population_lue_le = datetime.now(UTC)
    plans = [
        plan_cleanup(alert, only_degenerate=args.only_degenerate) for alert in alerts
    ]
    changed = sum(1 for plan in plans if plan.changed)

    if args.apply and changed:
        try:
            written = await persist(plans)
        except Exception as exc:
            print(f"{RED}Écriture impossible : {exc}{RESET}", file=sys.stderr)
            return EXIT_ERROR
        # Le rapport décrit ce qui a été lu ; le nombre écrit peut être plus petit si
        # une alerte a disparu entre la lecture et l'écriture.
        if written < changed:
            print(
                f"{RED}{changed - written} alerte(s) ont disparu entre la lecture "
                f"et l'écriture.{RESET}",
                file=sys.stderr,
            )

    if args.json:
        print(as_json(plans, apply=args.apply, cleaned_le=population_lue_le))
    else:
        print(
            render(
                plans,
                apply=args.apply,
                cleaned_le=population_lue_le,
                diff=args.diff,
            )
        )

    if args.apply:
        return EXIT_CLEAN
    return EXIT_CHANGES if changed else EXIT_CLEAN


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
