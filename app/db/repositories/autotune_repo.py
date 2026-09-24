"""Requêtes des faux positifs utilisés par l'auto-tuning."""

import uuid
from typing import Any

from sqlalchemy import delete, func, select

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch


async def get_alerts_with_dislikes(
    user_id: uuid.UUID,
    db_session: Any,
) -> list[tuple[Alert, int]]:
    """Alertes actives ayant au moins un faux positif signalé."""
    stmt = (
        select(Alert, func.count(func.distinct(Listing.id)).label("dislike_count"))
        .join(ListingMatch, ListingMatch.alert_id == Alert.id)
        .join(Listing, Listing.id == ListingMatch.listing_id)
        .join(BotEvent, BotEvent.listing_id == Listing.id)
        .where(
            Alert.user_id == user_id,
            Alert.status != AlertStatus.DELETED,
            BotEvent.event_type == "listing_disliked",
        )
        .group_by(Alert.id)
        .order_by(func.count(func.distinct(Listing.id)).desc(), Alert.created_at.desc())
    )
    res = await db_session.execute(stmt)
    return [(row[0], int(row[1])) for row in res.all()]


async def get_disliked_titles_for_alert(alert_id: uuid.UUID, db_session: Any) -> list[str]:
    """Titres des annonces marquées comme faux positifs pour une alerte."""
    stmt = (
        select(Listing.title)
        .join(ListingMatch, Listing.id == ListingMatch.listing_id)
        .join(BotEvent, BotEvent.listing_id == Listing.id)
        .where(
            ListingMatch.alert_id == alert_id,
            BotEvent.event_type == "listing_disliked",
        )
        .group_by(Listing.id, Listing.title)
        .order_by(func.max(BotEvent.created_at).desc())
        .limit(20)
    )
    res = await db_session.execute(stmt)
    titles = [row[0] for row in res.all() if row[0]]
    return titles


async def clear_dislikes_for_alert(alert_id: uuid.UUID, db_session: Any) -> int:
    """Supprime les faux positifs d'une alerte après application des exclusions."""
    listing_ids_subquery = (
        select(ListingMatch.listing_id)
        .where(ListingMatch.alert_id == alert_id)
        .scalar_subquery()
    )
    stmt = delete(BotEvent).where(
        BotEvent.event_type == "listing_disliked",
        (BotEvent.alert_id == alert_id) | (BotEvent.listing_id.in_(listing_ids_subquery)),
    )
    res = await db_session.execute(stmt)
    return res.rowcount or 0
