"""Tests unitaires pour les utilitaires purs et composants d'infrastructure core."""

import asyncio
import json
import logging

import pytest
from pydantic import SecretStr

from app.core.config import settings, validate_production_secrets
from app.core.http_client import (
    close_shared_http_client,
    get_shared_http_client,
)
from app.core.logging import JSONFormatter
from app.core.tasks import safe_create_task, wait_all_background_tasks
from app.utils.isbn import (
    extract_clean_isbn,
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_isbn13,
    normalize_isbn,
)
from app.utils.text import contains_keyword, contains_normalized_keyword, normalize_text


def test_json_formatter_redacts_secrets_in_message_and_extras():
    """Les secrets dans le message et les extras ne sortent pas dans le JSON."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="radar.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer super-secret-value token=%s",
        args=("abc123token",),
        exc_info=None,
    )
    record.bot_token = "123456:ABCDEF"
    record.request_id = "req-1"

    payload = json.loads(formatter.format(record))

    assert payload["request_id"] == "req-1"
    assert "super-secret-value" not in payload["message"]
    assert "abc123token" not in payload["message"]
    assert payload["bot_token"] == "[REDACTED]"
    assert "[REDACTED]" in payload["message"]


def test_production_startup_rejects_empty_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un démarrage production sans token d'accès s'arrête."""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", SecretStr(""))
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", SecretStr("secret"))
    monkeypatch.setattr(settings, "INGEST_API_TOKEN", SecretStr("ingest"))
    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", SecretStr("internal"))

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        validate_production_secrets()


def test_utils_text_contains_keyword():
    """Vérifie la détection exacte de mot-clé (mot unique et expression multi-mots)."""
    text = "BD 1984 Xavier Coste Sarbacane Très bon état"
    assert contains_keyword(text, "1984") is True
    assert contains_keyword(text, "coste") is True
    assert contains_keyword(text, "xavier coste") is True
    assert contains_keyword(text, "george orwell") is False
    assert contains_keyword(None, "1984") is False
    assert contains_keyword(text, None) is False


def test_utils_text_contains_normalized_keyword_matches_unnormalized_entrypoint():
    """Le chemin rapide et le chemin prudent doivent répondre pareil.

    `contains_normalized_keyword` reçoit un texte déjà normalisé et ne le renormalise
    pas : c'est ce qui a fait passer le matching de 341 à ~51 µs par évaluation. Le
    gain ne vaut que si les deux fonctions restent indiscernables sur les mêmes
    entrées — y compris les accents, la ponctuation et les mots partiels. Ce test
    verrouille cette équivalence ; il ne la prouve pas, il empêche qu'elle se casse.
    """
    normalized = "bd 1984 xavier coste sarbacane tres bon etat"
    # Le texte brut dont `normalized` est la normalisation
    raw = "BD 1984 — Xavier Coste : Sarbacane, Très bon état."
    keywords = [
        "1984",
        "coste",
        "xavier coste",
        "COSTE",
        "Très bon état",  # accent + espaces : doit normaliser le mot-clé
        "livre—complet",  # tiret cadratin dans le mot-clé
        "george orwell",  # absent
        "198",  # préfixe : ne doit pas matcher
        "sar",  # idem
        "état",  # accent
        "l'etat",  # apostrophe
    ]

    for keyword in keywords:
        assert contains_normalized_keyword(normalized, keyword) is (
            contains_keyword(raw, keyword)
        ), f"divergence sur le mot-clé {keyword!r}"

    assert normalize_text(normalized) == normalized, "le texte de test doit être normalisé"

    # Cas limites : entrées vides ou sans mots ne doivent jamais matcher ni lever.
    assert contains_normalized_keyword("", "1984") is False
    assert contains_normalized_keyword(None, "1984") is False
    assert contains_normalized_keyword(normalized, None) is False
    assert contains_normalized_keyword(normalized, "!!!") is False
    assert contains_normalized_keyword(normalized, "") is False


def test_utils_text_contains_normalized_keyword_is_not_fooled_by_substrings():
    """La frontière de mot reste entière sur le chemin rapide.

    Un mot-clé ne doit pas sscanfuer dans un mot plus long : « cos » ne trouve pas
    « coste ». C'est la raison d'être du `\b`, et elle doit tenir sur les deux chemins.
    """
    normalized = "bd blacksad tome 6 coste sarbacane"
    assert contains_normalized_keyword(normalized, "cos") is False
    assert contains_normalized_keyword(normalized, "coste") is True
    assert contains_normalized_keyword(normalized, "tome 6") is True
    assert contains_normalized_keyword(normalized, "tome") is True
    assert contains_normalized_keyword(normalized, "tome 7") is False
    # Expression multi-mots : les mots doivent être contigus, dans l'ordre.
    assert contains_normalized_keyword(normalized, "blacksad tome 6") is True
    assert contains_normalized_keyword(normalized, "tome blacksad") is False
    assert contains_normalized_keyword(normalized, "blacksad 6") is False


def test_utils_text_keyword_pattern_cache_reuses_pattern():
    """Le motif compilé d'un mot-clé est réutilisé, pas recompilé à chaque appel.

    C'est la seconde moitié du gain : la normalisation du mot-clé était déjà peu
    coûteuse, la recompilation du motif ne l'était pas.
    """
    from app.utils.text import _keyword_pattern

    first = _keyword_pattern("xavier coste")
    second = _keyword_pattern("Xavier—Costé")  # même mot-clé une fois normalisé
    assert first is not None
    assert first is second, "le motif doit être réutilisé, pas recompilé"
    assert first.pattern == r"\bxavier\s+coste\b"
    assert _keyword_pattern("!!!") is None, "un mot-clé sans lettre n'a pas de motif"


def test_utils_isbn_conversion_and_cleaning():
    """Vérifie la conversion et le nettoyage d'ISBNs complexes."""
    # ISBN-10 vers 13
    assert isbn10_to_isbn13("2070365034") == "9782070365036"
    assert isbn10_to_isbn13("2-070-36503-4") == "9782070365036"
    assert isbn10_to_isbn13("invalid") is None

    # Extraction d'ISBN-13 dans une phrase ou URL
    raw_text = "Vends BD Batman ISBN 978-2-36577-000-2 en très bon état"
    assert extract_clean_isbn(raw_text) == "9782365770002"

    # Extraction depuis chaîne brute compacte
    assert extract_clean_isbn("9782803672080") == "9782803672080"
    assert extract_clean_isbn("2070365034") == "9782070365036"
    assert extract_clean_isbn("") is None


def test_isbn_check_digit_and_normalization():
    """La clé de contrôle filtre les fautes de frappe, la normalisation tolère la ponctuation."""
    assert is_valid_isbn13("9782369903086") is True
    assert is_valid_isbn13("9782369903062") is True  # vrai ISBN, mais d'un autre ouvrage
    assert is_valid_isbn13("9782365770000") is False  # chiffre erroné
    assert is_valid_isbn13("97823699030") is False
    assert is_valid_isbn13(None) is False
    assert is_valid_isbn13("97823699030X6") is False

    assert is_valid_isbn10("2070365034") is True
    assert is_valid_isbn10("2070365038") is False
    assert is_valid_isbn10(None) is False

    assert normalize_isbn("978-2-36577-000-2") == "9782365770002"
    assert normalize_isbn(" 2-070-36503-4 ") == "2070365034"
    assert normalize_isbn("978236") is None
    assert normalize_isbn(None) is None


def test_extract_clean_isbn_rejects_invalid_check_digit():
    """Un ISBN mal saisi n'est jamais propagé : l'alerte ne doit pas matcher un mauvais livre."""
    # Clé de contrôle invalide
    assert extract_clean_isbn("9782365770000") is None
    assert extract_clean_isbn("2070365038") is None
    # Ni ISBN-10 ni ISBN-13 exploitables dans un texte
    assert extract_clean_isbn("prix 12 euros, très bon état") is None
    # Un code valide présent après un code invalide est retenu
    assert extract_clean_isbn("9782365770000 / 9782803672080") == "9782803672080"


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
    client3 = get_shared_http_client()
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


def test_fifo_set_ordering_and_trimming():
    """Vérifie que FifoSet maintient l'ordre FIFO et purge strictement les plus anciens."""
    from app.tasks.vinted_poller import FifoSet

    fifo = FifoSet(["a", "b", "c"], max_size=4, trim_size=2)
    assert len(fifo) == 3
    assert "a" in fifo
    assert "b" in fifo
    assert "c" in fifo

    # L'ajout d'un élément existant ne modifie ni la taille ni l'ordre
    fifo.add("b")
    assert len(fifo) == 3

    # Ajout du 4ème élément (atteint max_size)
    fifo.add("d")
    assert len(fifo) == 4

    # Ajout du 5ème élément (dépasse max_size=4, purge automatique vers trim_size=2)
    fifo.add("e")
    assert len(fifo) == 2
    # Les plus anciens (a, b, c) sont purgés, les plus récents (d, e) sont conservés
    assert "a" not in fifo
    assert "b" not in fifo
    assert "c" not in fifo
    assert "d" in fifo
    assert "e" in fifo


@pytest.mark.asyncio
async def test_redis_core_singleton_and_session():
    """Vérifie le cycle de vie du pool et du client Redis dans app.core.redis."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import app.core.redis as redis_mod

    # Réinitialisation de l'état du module pour le test
    redis_mod._redis_pool = None
    redis_mod._redis_client = None

    with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url:
        fake_pool = MagicMock()
        fake_pool.disconnect = AsyncMock()
        mock_from_url.return_value = fake_pool

        with patch("redis.asyncio.Redis") as mock_redis_cls:
            fake_client = MagicMock()
            fake_client.aclose = AsyncMock()
            mock_redis_cls.return_value = fake_client

            pool1 = redis_mod.get_redis_pool()
            pool2 = redis_mod.get_redis_pool()
            assert pool1 is pool2

            client1 = redis_mod.get_redis_client()
            client2 = redis_mod.get_redis_client()
            assert client1 is client2

            async with redis_mod.get_redis_session() as session_client:
                assert session_client is client1

            await redis_mod.close_redis()
            fake_client.aclose.assert_awaited_once()
            fake_pool.disconnect.assert_awaited_once()


def test_providers_accept_custom_http_client():
    """Vérifie que chaque provider et registry accepte l'injection d'un client HTTP personnalisé."""
    from unittest.mock import MagicMock

    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
    from app.providers.ebay_browse import EbayBrowseProvider
    from app.providers.gibert import GibertProvider
    from app.providers.momox import MomoxProvider
    from app.providers.recyclivre import RecyclivreProvider
    from app.providers.registry import ProviderRegistry
    from app.services.book_metadata_service import BookMetadataService

    dummy_client = MagicMock()

    momox = MomoxProvider(http_client=dummy_client)
    assert momox.http_client is dummy_client

    recyclivre = RecyclivreProvider(http_client=dummy_client)
    assert recyclivre.http_client is dummy_client

    gibert = GibertProvider(http_client=dummy_client)
    assert gibert.http_client is dummy_client

    ebay = EbayBrowseProvider(http_client=dummy_client)
    assert ebay.http_client is dummy_client

    chasse = ChasseAuxLivresProvider(http_client=dummy_client)
    assert chasse.http_client is dummy_client

    registry = ProviderRegistry(http_client=dummy_client)
    for p in registry.providers:
        if hasattr(p, "http_client"):
            assert p.http_client is dummy_client

    meta_service = BookMetadataService(http_client=dummy_client)
    assert meta_service.http_client is dummy_client


def test_settings_get_vinted_proxy():
    """Vérifie la résolution de proxy, conditionnée par son activation explicite."""
    from app.core.config import Settings

    # Aucun proxy
    s1 = Settings(VINTED_PROXY_ENABLED=False, VINTED_PROXY_URL=None)
    assert s1.get_vinted_proxy() is None

    # URL seule : rien n'est routé tant que le flag est faux
    s2 = Settings(VINTED_PROXY_ENABLED=False, VINTED_PROXY_URL="http://user:pass@1.2.3.4:8080")
    assert s2.get_vinted_proxy() is None

    # Proxy unique activé
    s3 = Settings(VINTED_PROXY_ENABLED=True, VINTED_PROXY_URL="http://user:pass@1.2.3.4:8080")
    assert s3.get_vinted_proxy() == "http://user:pass@1.2.3.4:8080"

    # Pool de proxys activé, séparés par virgules
    s4 = Settings(VINTED_PROXY_ENABLED=True, VINTED_PROXY_URL="http://proxy1:80, http://proxy2:80")
    assert s4.get_vinted_proxy() in ["http://proxy1:80", "http://proxy2:80"]
