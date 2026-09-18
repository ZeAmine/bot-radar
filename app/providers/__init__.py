"""Fournisseurs de prix de marché d'occasion."""

from app.providers.base import BookMarketProvider, MarketOffer
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.ebay_browse import EbayBrowseProvider
from app.providers.gibert import GibertProvider
from app.providers.momox import MomoxProvider
from app.providers.recyclivre import RecyclivreProvider
from app.providers.registry import ProviderRegistry

__all__ = [
    "BookMarketProvider",
    "MarketOffer",
    "ChasseAuxLivresProvider",
    "EbayBrowseProvider",
    "MomoxProvider",
    "RecyclivreProvider",
    "GibertProvider",
    "ProviderRegistry",
]
