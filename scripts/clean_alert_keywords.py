#!/usr/bin/env python3
"""Assainit les mots-clés dégénérés des alertes déjà enregistrées.

Le moteur IA a pu écrire des listes que rien ne bornait : une variante par suffixe de
lettre (« … 2a » … « … 2z ») sur un titre à numéro, une exclusion numérique nue
(« 1 ») qui rejette toute annonce contenant un chiffre. Le nettoyeur de
`app.domain.rules.keyword_hygiene` corrige la source ; ce script en applique le verdict
aux alertes **déjà stockées**, sans migration et sans toucher au code du bot.

Par défaut il ne fait que rapporter : c'est un `--dry-run` implicite. `--apply` réécrit
`variants` et `exclude_keywords` en base. Le nettoyage est **idempotent** : une seconde
exécution ne trouve plus rien à changer, parce que le nettoyeur est déjà sans effet sur
une liste assainie.

Le titre de chaque alerte sert de référence au nettoyeur : c'est lui qui décide qu'une
variante répète le titre ou qu'un suffixe s'ajoute à son numéro.

Usage :
    python scripts/clean_alert_keywords.py                    # rapport
    python scripts/clean_alert_keywords.py --apply            # réécrit
    python scripts/clean_alert_keywords.py --json             # sortie machine
    python scripts/clean_alert_keywords.py --limit 20
    python scripts/clean_alert_keywords.py --include-inactive

En production :
    docker compose exec -T app python scripts/clean_alert_keywords.py            # rapport
    docker compose exec -T app python scripts/clean_alert_keywords.py --apply    # nettoyage

Codes de sortie : 0 rien à nettoyer (ou nettoyage appliqué), 1 des alertes sont à
nettoyer en mode rapport, 2 erreur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.session import AsyncSessionLocal
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants

EXIT_CLEAN, EXIT_CHANGES, EXIT_ERROR = 0, 1, 2

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


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


def plan_cleanup(alert: Alert) -> AlertKeywordPlan:
    """Verdict du nettoyeur pour une alerte. Fonction pure : ni base, ni réseau."""
    variants = tuple(alert.variants or ())
    exclusions = tuple(alert.exclude_keywords or ())
    return AlertKeywordPlan(
        alert_id=str(alert.id),
        name=alert.name,
        variants_before=variants,
        variants_after=tuple(sanitize_variants(list(variants), alert.name)),
        exclusions_before=exclusions,
        exclusions_after=tuple(sanitize_exclusions(list(exclusions), alert.name)),
        inactive=alert.status != AlertStatus.ACTIVE,
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
) -> str:
    """Rapport lisible : en-tête daté, une ligne par alerte changée, un verdict final."""
    inactives = sum(1 for plan in plans if plan.inactive)
    mode = "écriture" if apply else "rapport (aucune écriture)"
    lines = [
        f"Nettoyage des mots-clés dégénérés — {format_instant(cleaned_le)}",
        f"Population lue : {len(plans)} alerte(s) (dont {inactives} inactive(s))",
        f"Mode : {mode}",
        "",
    ]

    changed = [plan for plan in plans if plan.changed]
    for plan in changed:
        head = f"{DIM}{plan.alert_id[:8]}{RESET} {plan.name}"
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
            f"{RED}{len(changed)} alerte(s) à nettoyer — relancer avec --apply.{RESET}"
        )
    return "\n".join(lines)


def as_json(plans: list[AlertKeywordPlan], *, apply: bool, cleaned_le: datetime) -> str:
    """La même information sans mise en forme, en-tête compris."""
    return json.dumps(
        {
            "genere_le": cleaned_le.astimezone(UTC).isoformat(),
            "mode": "ecriture" if apply else "rapport",
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
        help="réécrit les mots-clés en base (par défaut : rapport seul)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="rapport seul, explicite (c'est déjà le défaut)",
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
    plans = [plan_cleanup(alert) for alert in alerts]
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
        print(render(plans, apply=args.apply, cleaned_le=population_lue_le))

    if args.apply:
        return EXIT_CLEAN
    return EXIT_CHANGES if changed else EXIT_CLEAN


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
