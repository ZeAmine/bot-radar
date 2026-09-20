"""Sous-système IA pour Radar Bot (DeepSeek-V4.1-Flash)."""

from app.services.ai.client import DeepSeekClient, deepseek_client
from app.services.ai.pipeline import AIPostAlertPipeline, ai_post_alert_pipeline
from app.services.ai.registry import AIRegistry, ai_registry

__all__ = [
    "AIPostAlertPipeline",
    "AIRegistry",
    "DeepSeekClient",
    "ai_post_alert_pipeline",
    "ai_registry",
    "deepseek_client",
]
