import contextlib
import json
import logging
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import settings
from app.core.redis import get_redis_client
from app.services.ai.modules.base import BaseAIModule
from app.services.ai.schemas import AutoTuningPayload

logger = logging.getLogger(__name__)

# Cache mémoire pour les suggestions en attente
_pending_tuning: dict[str, list[str]] = {}

TUNING_SYSTEM_PROMPT = """Tu es l'analyste de précision des alertes BotRadar.
Tu reçois le mot-clé d'une alerte et une liste d'annonces marquées "faux positifs".

Identifie les termes récurrents parasites et suggère 2 à 4 mots-clés d'exclusion.
Exemple: pour 'Junk' avec faux positifs 'T-shirt Junk Food', suggérer: ['food', 't-shirt'].

Retourne STRICTEMENT un objet JSON :
{
  "suggested_exclusions": ["mot1", "mot2"],
  "explanation": "courte phrase expliquant pourquoi ces mots sont suggérés"
}"""


class AutoTuningModule(BaseAIModule):
    """Analyse les faux positifs signalés et propose des exclusions pertinentes."""


    @property
    def name(self) -> str:
        return "auto_tuning"

    @property
    def is_enabled(self) -> bool:
        return bool(settings.AI_ENABLED and settings.AI_FEATURE_AUTO_TUNING)

    async def suggest_exclusions(
        self, alert_name: str, false_positive_titles: list[str]
    ) -> tuple[list[str], str | None]:
        """Propose des mots-clés d'exclusion basés sur les annonces indésirables."""
        if not self.is_enabled or not self.client.is_configured or not false_positive_titles:
            return [], None

        user_prompt = (
            f"Alerte de l'utilisateur : '{alert_name}'\n"
            f"Annonces indésirables reçues :\n"
            + "\n".join(f"- {t}" for t in false_positive_titles[:15])
        )

        data = await self.client.generate_json(
            system_prompt=TUNING_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
        )
        if not isinstance(data, dict):
            return [], None
        try:
            payload = AutoTuningPayload.model_validate(data)
        except ValidationError:
            return [], None
        if not payload.suggested_exclusions:
            return [], None

        exclusions = [str(e).strip().lower() for e in payload.suggested_exclusions if e]
        return exclusions, payload.explanation


auto_tuning_module = AutoTuningModule()

# Nombre de faux positifs en dessous duquel aucune analyse n'est menée.
MIN_DISLIKES_FOR_PROPOSAL = 2


@dataclass(frozen=True)
class ExclusionProposal:
    """Exclusions réellement nouvelles, prêtes à être proposées à validation."""

    exclusions: list[str]
    explanation: str | None


async def build_exclusion_proposal(
    alert_name: str,
    already_excluded: list[str],
    disliked_titles: list[str],
    min_dislikes: int = MIN_DISLIKES_FOR_PROPOSAL,
) -> ExclusionProposal | None:
    """Construit la proposition d'exclusions, ou ``None`` s'il n'y a rien à dire.

    La règle est écrite une seule fois pour le scan planifié et pour l'analyse
    déclenchée par l'utilisateur : assez de faux positifs, un appel au moteur,
    puis le retrait de ce que l'alerte exclut déjà. ``None`` couvre les deux issues
    « pas assez de faux positifs » et « rien de neuf » : dans les deux cas il n'y a
    rien à proposer, et l'appelant décide seulement du message à afficher.

    Un seuil différent entre les deux appelants est un choix explicite, porté par
    ``min_dislikes``, et non une divergence accidentelle entre deux copies.
    """
    if len(disliked_titles) < min_dislikes:
        return None

    exclusions, explanation = await auto_tuning_module.suggest_exclusions(
        alert_name, disliked_titles
    )
    existing = {item.lower() for item in already_excluded}
    fresh = [item for item in exclusions if item.lower() not in existing]
    if not fresh:
        return None

    return ExclusionProposal(exclusions=fresh, explanation=explanation)


async def save_pending_tuning(alert_id: uuid.UUID, exclusions: list[str]) -> None:
    """Enregistre les exclusions suggérées en mémoire et dans Redis avec un TTL de 24h."""
    alert_key = str(alert_id)
    _pending_tuning[alert_key] = exclusions
    with contextlib.suppress(Exception):
        client = get_redis_client()
        await client.set(f"autotune:pending:{alert_key}", json.dumps(exclusions), ex=86400)


async def get_pending_tuning(alert_id: uuid.UUID) -> list[str]:
    """Récupère les exclusions suggérées depuis la mémoire ou Redis."""
    alert_key = str(alert_id)
    if alert_key in _pending_tuning:
        return _pending_tuning[alert_key]
    with contextlib.suppress(Exception):
        client = get_redis_client()
        raw = await client.get(f"autotune:pending:{alert_key}")
        if raw:
            items = json.loads(raw)
            if isinstance(items, list):
                _pending_tuning[alert_key] = items
                return items
    return []


async def pop_pending_tuning(alert_id: uuid.UUID) -> list[str]:
    """Extrait et supprime les exclusions suggérées (mémoire et Redis)."""
    alert_key = str(alert_id)
    exclusions = _pending_tuning.pop(alert_key, [])
    with contextlib.suppress(Exception):
        client = get_redis_client()
        raw = await client.get(f"autotune:pending:{alert_key}")
        if raw and not exclusions:
            items = json.loads(raw)
            if isinstance(items, list):
                exclusions = items
        await client.delete(f"autotune:pending:{alert_key}")
    return exclusions


def _cooldown_key(alert_id: uuid.UUID) -> str:
    return f"autotune:cooldown:{alert_id}"


async def claim_autotune_slot(alert_id: uuid.UUID, ttl_seconds: int = 86400) -> bool:
    """Réserve le créneau d'émission d'une proposition pour cette alerte.

    `SET ... NX` est atomique côté Redis : le scan planifié et le clic
    « Faux positif » peuvent démarrer la même analyse pour la même alerte, mais un
    seul obtient le créneau. Le contrôle précède l'appel au moteur : la fenêtre
    entre la lecture du cooldown et son écriture ne peut plus produire deux
    messages Telegram.

    Sans Redis, on refuse d'émettre. Le repli « passer quand même » transformerait
    une panne de cache en notifications en double.
    """
    try:
        client = get_redis_client()
    except Exception as exc:
        logger.warning("Client Redis indisponible pour le créneau Auto-Tuning : %s", exc)
        return False
    try:
        claimed = await client.set(_cooldown_key(alert_id), "1", ex=ttl_seconds, nx=True)
    except Exception as exc:
        logger.warning("Réservation du créneau Auto-Tuning impossible pour %s : %s", alert_id, exc)
        return False
    return bool(claimed)


async def release_autotune_claim(alert_id: uuid.UUID) -> None:
    """Relâche un créneau réservé alors qu'aucune proposition n'a été émise."""
    with contextlib.suppress(Exception):
        await get_redis_client().delete(_cooldown_key(alert_id))

