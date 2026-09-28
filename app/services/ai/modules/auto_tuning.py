import contextlib
import json
import logging
import uuid

from pydantic import ValidationError

from app.core.config import settings
from app.core.redis import get_redis_client
from app.services.ai.client import DeepSeekClient, deepseek_client
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

    def __init__(self, client: DeepSeekClient | None = None) -> None:
        self.client = client or deepseek_client

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


async def is_autotune_on_cooldown(alert_id: uuid.UUID) -> bool:
    """Vérifie si une proposition proactive récente a déjà été émise pour cette alerte."""
    with contextlib.suppress(Exception):
        client = get_redis_client()
        return bool(await client.exists(f"autotune:cooldown:{alert_id}"))
    return False


async def set_autotune_cooldown(alert_id: uuid.UUID, ttl_seconds: int = 86400) -> None:
    """Active un temps de refroidissement pour éviter de spammer l'utilisateur (défaut: 24h)."""
    with contextlib.suppress(Exception):
        client = get_redis_client()
        await client.set(f"autotune:cooldown:{alert_id}", "1", ex=ttl_seconds)

