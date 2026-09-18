"""Tests unitaires pour les utilitaires purs et composants d'infrastructure core."""

import asyncio

import pytest

from app.core.http_client import (
    close_shared_http_client,
    get_shared_http_client,
    http_client_lifespan,
)
from app.core.tasks import safe_create_task, wait_all_background_tasks
from app.utils.isbn import extract_clean_isbn, isbn10_to_isbn13
from app.utils.text import contains_keyword


def test_utils_text_contains_keyword():
    """Vérifie la détection exacte de mot-clé (mot unique et expression multi-mots)."""
    text = "BD 1984 Xavier Coste Sarbacane Très bon état"
    assert contains_keyword(text, "1984") is True
    assert contains_keyword(text, "coste") is True
    assert contains_keyword(text, "xavier coste") is True
    assert contains_keyword(text, "george orwell") is False
    assert contains_keyword(None, "1984") is False
    assert contains_keyword(text, None) is False


def test_utils_isbn_conversion_and_cleaning():
    """Vérifie la conversion et le nettoyage d'ISBNs complexes."""
    # ISBN-10 vers 13
    assert isbn10_to_isbn13("220507805X") == "9782205078053"
    assert isbn10_to_isbn13("2-205-07805-X") == "9782205078053"
    assert isbn10_to_isbn13("invalid") is None

    # Extraction d'ISBN-13 dans une phrase ou URL
    raw_text = "Vends BD Batman ISBN 978-2-36577-000-0 en très bon état"
    assert extract_clean_isbn(raw_text) == "9782365770000"

    # Extraction depuis chaîne brute compacte
    assert extract_clean_isbn("9782803672080") == "9782803672080"
    assert extract_clean_isbn("220507805X") == "9782205078053"
    assert extract_clean_isbn("") is None


@pytest.mark.asyncio
async def test_shared_http_client_lifecycle():
    """Vérifie que le client HTTP partagé est réutilisé et fermé proprement."""
    client1 = get_shared_http_client()
    client2 = get_shared_http_client()
    assert client1 is client2
    assert not client1.is_closed

    await close_shared_http_client()
    assert client1.is_closed

    # Réouverture paresseuse automatique
    async with http_client_lifespan() as client3:
        assert not client3.is_closed

    await close_shared_http_client()


@pytest.mark.asyncio
async def test_safe_create_task_handles_exceptions_and_cleanup():
    """Vérifie que safe_create_task prévient la collecte GC et gère la terminaison."""
    executed = False

    async def sample_task():
        nonlocal executed
        await asyncio.sleep(0.01)
        executed = True
        return 42

    task = safe_create_task(sample_task(), name="test_sample")
    await wait_all_background_tasks(timeout=1.0)
    assert executed is True
    assert task.done()
    assert task.result() == 42


@pytest.mark.asyncio
async def test_safe_create_task_with_exception():
    """Vérifie qu'une tâche avec exception est proprement nettoyée sans crasher."""
    async def failing_task():
        await asyncio.sleep(0.01)
        raise ValueError("Erreur intentionnelle de test")

    task = safe_create_task(failing_task(), name="test_failing")
    await wait_all_background_tasks(timeout=1.0)
    assert task.done()
    assert isinstance(task.exception(), ValueError)
