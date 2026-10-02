"""Sous-système IA de BotRadar : conseil, jamais blocage.

Les modules de `modules/` utilisent le client fourni par `app/providers/llm/`. Aucun
d'eux ne connaît le moteur : changer de modèle est une variable d'environnement.
"""

from app.services.ai.pipeline import AIPostAlertPipeline, ai_post_alert_pipeline
from app.services.ai.registry import AIRegistry, ai_registry

__all__ = [
    "AIPostAlertPipeline",
    "AIRegistry",
    "ai_post_alert_pipeline",
    "ai_registry",
]
