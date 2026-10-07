"""Le câblage du parcours `/add` : les espions, et les patchs qui les installent.

Un test de parcours ne remplace pas les frontières une à une — il branche **toutes**
celles que la chaîne traverse, au même endroit, pour qu'aucune ne parte en réseau ni
n'atteigne la base. C'est ce que fait `patched()` : la liste des noms de production
remplacés, et les espions qui les remplacent. Les doubles eux-mêmes vivent dans
`tests/add_fakes.py` ; les tâches de fond que le handler croit lancer sont retenues
ici, pour que le test décide quand elles tournent.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from tests.add_fakes import UnloadedBook, cached_market_price, fake_book_search


def repositories(
    isbn13: str,
    *,
    already_catalogued: bool = False,
    lazy_created_book: bool = False,
) -> dict:
    """Repositories simulées et spies pour `save_alert`.

    `already_catalogued` décrit un livre déjà au catalogue et déjà coté : c'est le
    cas où la confirmation relit un prix après la collecte, même si elle échoue.
    `lazy_created_book` décrit le livre qui vient d'être créé : `create` rend un objet
    dont `market_cache` n'est pas chargé, et `get_by_id` le relit comme en production.
    """
    book = MagicMock()
    book.id = uuid.uuid4()
    book.isbn13 = isbn13
    book.market_cache = cached_market_price() if already_catalogued else None

    alert = MagicMock()
    alert.id = uuid.uuid4()

    created = UnloadedBook(isbn13) if lazy_created_book else book
    book_repo = MagicMock()
    book_repo.get_by_isbn13 = AsyncMock(return_value=book if already_catalogued else None)
    book_repo.create = AsyncMock(return_value=created)
    book_repo.get_by_id = AsyncMock(return_value=book)
    book_repo.link_alert_to_book = AsyncMock()
    book_repo.get_all_books_for_alert = AsyncMock(return_value=[])

    alert_repo = MagicMock()
    alert_repo.create = AsyncMock(return_value=alert)
    alert_repo.find_existing = AsyncMock(return_value=None)

    category_repo = MagicMock()
    category_repo.get_by_id = AsyncMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    return {
        "book_repo": book_repo,
        "alert_repo": alert_repo,
        "category_repo": category_repo,
        # La création attend la cotation avant d'activer la surveillance.
        "price_now": AsyncMock(return_value=None),
        # La recherche des variantes part en arrière-plan : le test la déclenche lui-même.
        "tasks": [],
    }


def defer_task(holder: list) -> object:
    """Remplace `safe_create_task` : retient la coroutine pour que le test la pilote."""

    def _run(coro, name=None):
        holder.append((coro, name))
        return None

    return _run


async def drain(tasks: list) -> None:
    """Exécute la recherche de fond retenue, comme le ferait la boucle d'événements."""
    for coro, _name in tasks:
        await coro
    tasks.clear()


def patched(
    repos: dict,
    bnf: MagicMock,
    client: MagicMock,
    *,
    isbn_bnf: MagicMock | None = None,
):
    """Patches de toutes les frontières du flux `/add`.

    `bnf` sert à la résolution par le moteur IA (`natural_add`) ; `isbn_bnf`,
    à la vérification d'un ISBN saisi à la main (`alerts_helpers`). Les deux
    frontières reçoivent le même faux par défaut, mais un parcours qui leur
    donne des verdicts différents le dit ici plutôt qu'en patch imbriqué —
    dont l'ordre de démarrage décide du faux qui l'emporte.
    """
    if isbn_bnf is None:
        isbn_bnf = bnf
    return (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch("app.services.ai.modules.natural_add.natural_add_module.client", client),
        patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ),
        # Les callbacks de sélection (œuvre, édition) revalident leur ISBN : ils
        # consomment le même cas d'usage, dans leur propre module.
        patch(
            "app.bot.handlers.alerts_create_confirm.BookSearchUseCase",
            fake_book_search(bnf),
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module."
            "generate_variants_and_exclusions",
            new_callable=AsyncMock,
            return_value=KeywordVariantsOutcome(
                variants=["couleurdeschoses"],
                exclusions=["keeping two"],
                answered=True,
            ),
        ),
        # La vérification d'un ISBN saisi à la main passe par `alerts_helpers`, pas
        # par `natural_add` : sans ce double, le parcours dépend du vrai réseau BNF
        # (et d'un client HTTP partagé lié à la boucle d'un autre test). Depuis le
        # contrat partagé, ce joint est le cas d'usage, pas le service.
        patch(
            "app.bot.handlers.alerts_helpers.BookSearchUseCase",
            fake_book_search(isbn_bnf),
        ),
        patch(
            "app.bot.handlers.alerts_create.safe_create_task",
            new=defer_task(repos["tasks"]),
        ),
        patch("app.bot.handlers.alerts_create.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts_create.AlertRepository", return_value=repos["alert_repo"]),
        patch(
            "app.bot.handlers.alerts_create.CategoryRepository",
            return_value=repos["category_repo"],
        ),
        patch(
            "app.bot.handlers.alerts_create.price_book_now",
            new=repos["price_now"],
        ),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=repos["alert_repo"]),
        patch("app.bot.handlers.alerts.BookRepository", return_value=repos["book_repo"]),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=repos["category_repo"]),
    )
