"""Routeur d'agrégation et façade pour les flux d'alertes Telegram.

Ce module regroupe les sous-modules spécialisés :
- alerts_helpers : utilitaires de formatage et parsing semi-automatique
- alerts_create : flux de création d'alertes (/add)
- alerts_view : consultation et pagination (/show, /list)
- alerts_manage : modification, pause, reprise et suppression (/edit, /pause, /resume, /delete)
"""

from aiogram import Router

# Ré-export du flux de création
from app.bot.handlers._alerts_router import router as alerts_create_router
from app.bot.handlers.alerts_create import (
    handle_category_selected,
    handle_query_input,
    handle_update_existing_alert_price,
    process_max_price_input,
    save_alert,
    start_add_alert,
)
from app.bot.handlers.alerts_create_confirm import (
    back_to_recap,
    cancel_alert_creation,
    cancel_router,
    edit_alert_creation_prompt,
    handle_cancel_command,
    prompt_new_price,
)

# Ré-export des aides et formateurs
from app.bot.handlers.alerts_helpers import (
    extract_inline_price_and_clean_query,
    format_alerts_list_text,
    process_bd_query_lookup,
    process_tech_or_clothes_query,
)

# Ré-export du flux de gestion / modification
from app.bot.handlers.alerts_manage import (
    confirm_soft_delete_alert,
    handle_close_alert_menu,
    handle_delete_command,
    handle_edit_category_menu,
    handle_edit_command,
    handle_pause_command,
    handle_resume_command,
    handle_select_edit_category,
    prompt_delete_confirmation,
    prompt_new_field_value,
    save_edited_field_value,
    select_edit_field,
    show_edit_categories,
    toggle_pause_alert,
    toggle_resume_alert,
)
from app.bot.handlers.alerts_manage import (
    router as alerts_manage_router,
)

# Ré-export du flux de consultation
from app.bot.handlers.alerts_view import (
    CATEGORY_ARG_MAP,
    display_alerts_list,
    handle_list_category_menu,
    handle_select_list_category,
    list_alerts,
    paginate_alerts,
    paginate_alerts_list,
    show_alerts,
    show_list_categories,
    view_single_alert,
)
from app.bot.handlers.alerts_view import (
    router as alerts_view_router,
)

# Ré-export des classes de repository pour rétrocompatibilité et isolation des tests unitaires
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.category_repo import CategoryRepository
from app.db.repositories.market_price_repo import MarketPriceRepository

# Ré-export des utilitaires ISBN
from app.utils.isbn import extract_clean_isbn, isbn10_to_isbn13

# Routeur principal d'agrégation. L'annulation est incluse en premier : elle doit
# être arbitrée avant tout handler d'état qui accepte du texte libre, sinon `/cancel`
# serait consommé comme un titre.
router = Router(name="alerts_router")
router.include_router(cancel_router)
router.include_router(alerts_create_router)
router.include_router(alerts_view_router)
router.include_router(alerts_manage_router)

__all__ = [
    # Router
    "router",
    "cancel_router",
    "alerts_create_router",
    "alerts_view_router",
    "alerts_manage_router",
    # Repositories
    "AlertRepository",
    "CategoryRepository",
    "BookRepository",
    "MarketPriceRepository",
    # ISBN
    "extract_clean_isbn",
    "isbn10_to_isbn13",
    # Helpers
    "extract_inline_price_and_clean_query",
    "format_alerts_list_text",
    "process_bd_query_lookup",
    "process_tech_or_clothes_query",
    # Create
    "start_add_alert",
    "handle_category_selected",
    "handle_query_input",
    "handle_update_existing_alert_price",
    "process_max_price_input",
    "save_alert",
    "handle_cancel_command",
    "cancel_alert_creation",
    "edit_alert_creation_prompt",
    "prompt_new_price",
    "back_to_recap",
    # View
    "CATEGORY_ARG_MAP",
    "show_alerts",
    "paginate_alerts",
    "view_single_alert",
    "show_list_categories",
    "display_alerts_list",
    "list_alerts",
    "handle_list_category_menu",
    "handle_select_list_category",
    "paginate_alerts_list",
    # Manage
    "handle_edit_command",
    "show_edit_categories",
    "handle_edit_category_menu",
    "handle_select_edit_category",
    "handle_close_alert_menu",
    "select_edit_field",
    "prompt_new_field_value",
    "save_edited_field_value",
    "handle_pause_command",
    "toggle_pause_alert",
    "handle_resume_command",
    "toggle_resume_alert",
    "handle_delete_command",
    "prompt_delete_confirmation",
    "confirm_soft_delete_alert",
]
