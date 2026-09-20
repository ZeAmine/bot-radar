"""Classe de base pour tous les modules IA autonomes (Plugin Pattern)."""

from abc import ABC, abstractmethod
from typing import Any

from app.db.models.listing import Listing


class BaseAIModule(ABC):
    """Interface abstraite que chaque use case IA doit implémenter."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nom unique identifiant le module."""
        ...

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Indique si le module est activé selon les réglages."""
        ...


class BasePostAlertModule(BaseAIModule):
    """Interface spécifique aux modules d'enrichissement asynchrone post-alerte (Phase 2)."""

    @abstractmethod
    def build_prompt_chunk(self, listing: Listing, alert: Any) -> tuple[str, str] | None:
        """Retourne (clé_json_attendue, instruction_prompt) pour l'appel combiné.

        Si le module n'a rien à analyser pour cette annonce, retourne None.
        """
        ...

    @abstractmethod
    def parse_chunk_result(self, raw_chunk: Any, listing: Listing, alert: Any) -> Any:
        """Parse la portion de JSON retournée par DeepSeek en objet de résultat typé."""
        ...

    @abstractmethod
    def format_telegram_html(self, result: Any) -> str | None:
        """Génère la ligne de texte HTML à afficher dans la notification enrichie.

        Retourne None si aucun message d'alerte n'est nécessaire.
        """
        ...
