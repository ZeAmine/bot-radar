"""Requêtes des faux positifs utilisés par l'auto-tuning."""

import uuid
from typing import Any

from sqlalchemy import delete, func, select

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User


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


async def record_listing_dislike(
    db_session: Any,
    *,
    listing_id: uuid.UUID,
    alert_id: uuid.UUID | None,
    chat_id: int | None,
) -> bool:
    """Journalise une seule fois un signalement pour cette annonce et cette alerte."""
    # Le verrou de ligne sérialise les confirmations concurrentes en production.
    await db_session.execute(select(Listing.id).where(Listing.id == listing_id).with_for_update())
    # L'identifiant Telegram dépasse int32 : un CAST AS INTEGER fait refuser
    # le paramètre par asyncpg avant toute réponse au bouton « Confirmer ».
    chat_id_value = BotEvent.metadata_["chat_id"].as_string()
    same_chat = chat_id_value.is_(None) if chat_id is None else chat_id_value == str(chat_id)
    existing = await db_session.execute(
        select(BotEvent.id)
        .where(
            BotEvent.event_type == "listing_disliked",
            BotEvent.listing_id == listing_id,
            BotEvent.alert_id == alert_id,
            same_chat,
        )
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return False
    db_session.add(
        BotEvent(
            event_type="listing_disliked",
            listing_id=listing_id,
            alert_id=alert_id,
            metadata_={"chat_id": chat_id},
        )
    )
    return True


async def clear_dislikes_for_alert(alert_id: uuid.UUID, db_session: Any) -> int:
    """Supprime les faux positifs d'une alerte après application des exclusions."""
    listing_ids_subquery = (
        select(ListingMatch.listing_id).where(ListingMatch.alert_id == alert_id).scalar_subquery()
    )
    stmt = delete(BotEvent).where(
        BotEvent.event_type == "listing_disliked",
        (BotEvent.alert_id == alert_id) | (BotEvent.listing_id.in_(listing_ids_subquery)),
    )
    res = await db_session.execute(stmt)
    return res.rowcount or 0


async def get_dislike_count_for_alert(alert_id: uuid.UUID, db_session: Any) -> int:
    """Retourne le nombre de faux positifs distincts enregistrés pour une alerte."""
    stmt = (
        select(func.count(func.distinct(Listing.id)))
        .join(ListingMatch, ListingMatch.listing_id == Listing.id)
        .join(BotEvent, BotEvent.listing_id == Listing.id)
        .where(
            ListingMatch.alert_id == alert_id,
            BotEvent.event_type == "listing_disliked",
        )
    )
    res = await db_session.execute(stmt)
    return int(res.scalar() or 0)


async def get_all_alerts_with_dislike_threshold(
    min_dislikes: int,
    db_session: Any,
) -> list[tuple[Alert, User, int]]:
    """Retourne toutes les alertes actives ayant au moins min_dislikes faux positifs."""
    stmt = (
        select(Alert, User, func.count(func.distinct(Listing.id)).label("dislike_count"))
        .join(User, User.id == Alert.user_id)
        .join(ListingMatch, ListingMatch.alert_id == Alert.id)
        .join(Listing, Listing.id == ListingMatch.listing_id)
        .join(BotEvent, BotEvent.listing_id == Listing.id)
        .where(
            Alert.status != AlertStatus.DELETED,
            User.is_allowed.is_(True),
            BotEvent.event_type == "listing_disliked",
        )
        .group_by(Alert.id, User.id)
        .having(func.count(func.distinct(Listing.id)) >= min_dislikes)
        .order_by(func.count(func.distinct(Listing.id)).desc())
    )
    res = await db_session.execute(stmt)
    return [(row[0], row[1], int(row[2])) for row in res.all()]
