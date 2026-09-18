"""États FSM pour la création et l'édition des alertes."""

from aiogram.fsm.state import State, StatesGroup


class AlertCreationState(StatesGroup):
    """Machine à états pour la création conversationnelle d'une alerte (/add)."""

    waiting_for_query = State()
    waiting_for_url = State()
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_keywords = State()
    waiting_for_exclude_keywords = State()
    waiting_for_variants = State()
    waiting_for_max_price = State()
    waiting_for_conditions = State()
    confirm_creation = State()


class AlertEditState(StatesGroup):
    """Machine à états pour la modification d'un champ d'une alerte (/edit)."""

    selecting_field = State()
    waiting_for_new_value = State()
