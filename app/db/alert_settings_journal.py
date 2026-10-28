"""Écriture unique du journal des réglages d'alerte, depuis la persistance.

Un seul point enregistre : l'événement `before_flush` de la session. Tout chemin qui
modifie un réglage d'alerte — handler du bot, tâche de fond, script de masse — est
donc couvert sans que son auteur ait à y penser. Le champ, l'ancienne valeur et la
nouvelle viennent de l'historique d'attribut que SQLAlchemy tient déjà ; une
affectation sans changement n'a pas d'historique et n'écrit rien.

L'origine est le module et la fonction qui ont posé la valeur, relevés au moment de
l'affectation : c'est ce qui distingue une action du bot d'une modification de masse
ou d'un script, sans dépendre de la pile au moment du `commit`.
"""

from __future__ import annotations

import sys
from types import FrameType
from typing import Any

from sqlalchemy import event
from sqlalchemy import inspect as _inspect
from sqlalchemy.orm import Session, object_session

from app.db.models.alert import Alert
from app.db.models.alert_setting_change import AlertSettingChange

JOURNALED_FIELDS: tuple[str, ...] = (
    "include_keywords",
    "exclude_keywords",
    "variants",
    "max_item_price_cents",
    "accepted_conditions",
    "listing_baseline",
)

UNATTRIBUTED_ORIGIN = "unattributed"

_ORIGINS_KEY = "alert_setting_change_origins"
_IGNORED_MODULE_PREFIXES = ("sqlalchemy", __name__)

_installed = False


def _calling_origin() -> str:
    """Module et fonction de l'appelant hors de SQLAlchemy et hors de ce module."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module and not module.startswith(_IGNORED_MODULE_PREFIXES):
            code = frame.f_code
            return f"{module}.{getattr(code, 'co_qualname', code.co_name)}"
        frame = frame.f_back
    return UNATTRIBUTED_ORIGIN


def _remember_origin(target: Any, _value: Any, _oldvalue: Any, _initiator: Any) -> None:
    """Retient qui pose la valeur ; le flush consomme et vide ce relevé."""
    session = object_session(target)
    if session is None:
        return
    session.info.setdefault(_ORIGINS_KEY, {})[id(target)] = _calling_origin()


def journal_alert_setting_changes(
    session: Session,
    _flush_context: Any = None,
    _instances: Any = None,
) -> None:
    """Écrit une ligne par champ de réglage réellement modifié sur une alerte."""
    origins: dict[int, str] = session.info.pop(_ORIGINS_KEY, {})
    for alert in list(session.dirty):
        if not isinstance(alert, Alert) or alert in session.deleted:
            continue
        state = _inspect(alert)
        origin = origins.get(id(alert), UNATTRIBUTED_ORIGIN)
        for field in JOURNALED_FIELDS:
            history = state.attrs[field].history
            if not history.has_changes():
                continue
            old_value = history.deleted[0] if history.deleted else None
            new_value = history.added[0] if history.added else None
            if old_value == new_value:
                continue
            session.add(
                AlertSettingChange(
                    alert_id=alert.id,
                    field=field,
                    old_value=old_value,
                    new_value=new_value,
                    origin=origin,
                )
            )


def install() -> None:
    """Branche le relevé d'origine et l'écriture du journal, une seule fois."""
    global _installed
    if _installed:
        return
    for field in JOURNALED_FIELDS:
        event.listen(
            getattr(Alert, field),
            "set",
            _remember_origin,
            active_history=True,
        )
    event.listen(Session, "before_flush", journal_alert_setting_changes)
    _installed = True


install()
