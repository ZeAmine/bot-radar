"""Registre central des modules IA (Plugin Pattern)."""

import logging

from app.services.ai.modules.auto_tuning import auto_tuning_module
from app.services.ai.modules.base import BaseAIModule, BasePostAlertModule
from app.services.ai.modules.bundle_decomposition import bundle_decomposition_module
from app.services.ai.modules.hidden_condition import hidden_condition_module
from app.services.ai.modules.keyword_variants import keyword_variants_module
from app.services.ai.modules.natural_add import natural_add_module
from app.services.ai.modules.negotiation import negotiation_module
from app.services.ai.modules.price_glitch import price_glitch_module
from app.services.ai.modules.profit_scoring import profit_scoring_module
from app.services.ai.modules.vision_guard import vision_guard_module

logger = logging.getLogger(__name__)


class AIRegistry:
    """Gère l'enregistrement, la découverte et l'activation des modules IA."""

    def __init__(self) -> None:
        self._all_modules: dict[str, BaseAIModule] = {}
        self._post_alert_modules: dict[str, BasePostAlertModule] = {}

        # Enregistrement des modules disponibles
        self.register(natural_add_module)
        self.register(hidden_condition_module)
        self.register(price_glitch_module)
        self.register(bundle_decomposition_module)
        self.register(profit_scoring_module)
        self.register(vision_guard_module)
        self.register(negotiation_module)
        self.register(keyword_variants_module)
        self.register(auto_tuning_module)

    def register(self, module: BaseAIModule) -> None:
        """Enregistre un module dans le registre."""
        self._all_modules[module.name] = module
        if isinstance(module, BasePostAlertModule):
            self._post_alert_modules[module.name] = module
        logger.debug("Module IA enregistré : %s", module.name)

    def unregister(self, module_name: str) -> None:
        """Désenregistre un module."""
        self._all_modules.pop(module_name, None)
        self._post_alert_modules.pop(module_name, None)

    def get_module(self, name: str) -> BaseAIModule | None:
        """Retourne un module par son nom."""
        return self._all_modules.get(name)

    def get_active_post_alert_modules(self) -> list[BasePostAlertModule]:
        """Retourne tous les modules d'enrichissement post-alerte activés."""
        return [m for m in self._post_alert_modules.values() if m.is_enabled]


# Instance singleton du registre
ai_registry = AIRegistry()
