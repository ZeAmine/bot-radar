"""États FSM pour la création et l'édition des alertes."""

from aiogram.fsm.state import State, StatesGroup


class AlertCreationState(StatesGroup):
    """Machine à états pour la création conversationnelle d'une alerte (/add)."""

    waiting_for_query = State()
    waiting_for_category = State()
    waiting_for_max_price = State()
    confirm_creation = State()
    waiting_for_edit_field = State()


class AlertEditState(StatesGroup):
    """Machine à états pour la modification d'un champ d'une alerte (/edit)."""

    waiting_for_new_value = State()


class AutoTuneState(StatesGroup):
    """Machine à états pour la personnalisation manuelle des exclusions Auto-Tuning."""

    waiting_for_custom_exclusions = State()
