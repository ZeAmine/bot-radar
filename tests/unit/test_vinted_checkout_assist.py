"""Le parcours local d'achat assisté : lien marqué, clavier, contrat du script.

Le userscript lui-même est éprouvé par `tests/js/checkout_assist_harness.mjs`
(logique pure, exécutée par Node) ; ici on verrouille le lien que Telegram
émet, le clavier qui le porte, et l'accord entre les deux artefacts. Aucun
navigateur réel n'est ouvert, aucune page Vinted n'est contactée.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.bot.keyboards.listings import (
    CHECKOUT_ASSIST_MARKER,
    assisted_checkout_url,
    get_listing_alert_keyboard,
    listing_alert_keyboard,
)
from app.core.config import settings
from app.db.models.listing import Listing

REPO_ROOT = Path(__file__).resolve().parents[2]
USERSCRIPT = REPO_ROOT / "scripts" / "vinted_checkout_assist.user.js"
HARNESS = REPO_ROOT / "tests" / "js" / "checkout_assist_harness.mjs"


def sample_listing(
    *,
    external_id: str = "10373403907",
    source_name: str = "vinted",
) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        source_name=source_name,
        external_id=external_id,
        url=f"https://www.vinted.fr/items/{external_id}-console",
        title="Console",
        normalized_title="console",
        category_slug="tech",
        item_price_cents=1000,
        raw_payload={},
    )


# ---------------------------------------------------------------------------
# Le lien assisté
# ---------------------------------------------------------------------------


def test_assisted_url_carries_the_marker_and_the_item_id() -> None:
    url = assisted_checkout_url(
        "https://www.vinted.fr/items/10373403907-console", "10373403907", "vinted"
    )
    assert url == (
        "https://www.vinted.fr/items/10373403907-console"
        f"#{CHECKOUT_ASSIST_MARKER}=10373403907"
    )


def test_assisted_url_replaces_an_existing_fragment() -> None:
    """Le marqueur n'est jamais concaténé à un fragment existant."""
    url = assisted_checkout_url(
        "https://www.vinted.fr/items/1-console#autre", "1", "vinted"
    )
    assert url is not None
    assert url.count("#") == 1
    assert url.endswith(f"#{CHECKOUT_ASSIST_MARKER}=1")


@pytest.mark.parametrize(
    ("listing_url", "external_id", "source_name"),
    [
        ("https://www.vinted.fr/items/1", "abc", "vinted"),
        ("https://www.vinted.fr/items/1", "1", "other-source"),
        ("https://www.vinted.fr/items/1", "", "vinted"),
    ],
)
def test_assisted_url_is_none_when_it_cannot_target_an_item(
    listing_url: str, external_id: str, source_name: str
) -> None:
    assert assisted_checkout_url(listing_url, external_id, source_name) is None


# ---------------------------------------------------------------------------
# Le clavier : deux formes pour « Acheter », jamais les deux à la fois
# ---------------------------------------------------------------------------


def test_keyboard_uses_the_assisted_link_by_default() -> None:
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/1-console"
    keyboard = get_listing_alert_keyboard(
        listing_id,
        url,
        "tech",
        assisted_checkout_url=f"{url}#{CHECKOUT_ASSIST_MARKER}=1",
    )
    rows = keyboard.inline_keyboard
    buy, view = rows[0]
    assert buy.text == "Acheter"
    assert buy.url == f"{url}#{CHECKOUT_ASSIST_MARKER}=1"
    assert buy.callback_data is None
    assert view.text == "Voir l'annonce"
    assert view.url == url


def test_keyboard_prefers_the_server_callback_when_enabled() -> None:
    """Quand la préparation serveur est active, elle prend la main : un seul bouton."""
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/1-console"
    keyboard = get_listing_alert_keyboard(
        listing_id,
        url,
        "tech",
        checkout_enabled=True,
        assisted_checkout_url=f"{url}#{CHECKOUT_ASSIST_MARKER}=1",
    )
    rows = keyboard.inline_keyboard
    assert [button.text for button in rows[0]] == ["Acheter", "Voir l'annonce"]
    assert rows[0][0].callback_data == f"buy:{listing_id}"
    assert rows[0][0].url is None


def test_keyboard_has_no_buy_action_without_either_source() -> None:
    keyboard = get_listing_alert_keyboard(uuid.uuid4(), "https://www.vinted.fr/items/1", "tech")
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Acheter" not in texts
    assert "Voir l'annonce" in texts


def test_listing_keyboard_adapter_switches_on_the_server_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un seul endroit décide : le flag serveur, sinon le lien assisté."""
    listing = sample_listing()

    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    server = listing_alert_keyboard(listing)
    assert server.inline_keyboard[0][0].callback_data == f"buy:{listing.id}"
    assert server.inline_keyboard[0][0].url is None

    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", False)
    assisted = listing_alert_keyboard(listing)
    buy = assisted.inline_keyboard[0][0]
    assert buy.callback_data is None
    assert buy.url == listing.url + f"#{CHECKOUT_ASSIST_MARKER}={listing.external_id}"


def test_listing_keyboard_adapter_ignores_a_foreign_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", False)
    keyboard = listing_alert_keyboard(sample_listing(source_name="other-source"))
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Acheter" not in texts


# ---------------------------------------------------------------------------
# Contrat entre le lien Telegram et le userscript
# ---------------------------------------------------------------------------


def _userscript() -> str:
    return USERSCRIPT.read_text(encoding="utf-8")


def _userscript_code() -> str:
    """Le script sans ses commentaires : les mots du texte ne sont pas du code."""
    without_blocks = re.sub(r"/\*.*?\*/", "", _userscript(), flags=re.S)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


def test_userscript_exists_and_is_scoped_to_item_pages() -> None:
    source = _userscript()
    matches = re.findall(r"// @match\s+(\S+)", source)
    assert matches, "le userscript doit déclarer ses pages"
    assert all("/items/" in match for match in matches)
    # Jamais la page de checkout : rien du parcours de paiement n'est pris en charge.
    assert not any("/checkout" in match for match in matches)


def test_userscript_marker_agrees_with_the_keyboard_constant() -> None:
    """Les deux artefacts doivent porter le même marqueur, sinon rien ne se parle."""
    declared = re.search(r"const MARKER = '([^']+)'", _userscript())
    assert declared is not None
    assert declared.group(1) == CHECKOUT_ASSIST_MARKER


def test_userscript_never_touches_delivery_or_payment() -> None:
    """Le script ne connaît ni point relais, ni paiement, ni confirmation."""
    code = _userscript_code().lower()
    for forbidden in (
        "chronopost",
        "mondial",
        "point relais",
        "points relais",
        "pickup",
        "payer",
        "payment",
        "confirmer",
        "checkout",
    ):
        assert not re.search(rf"\b{re.escape(forbidden)}\b", code), (
            f"le userscript ne doit pas parler de {forbidden!r} dans son code"
        )


def test_userscript_makes_no_network_or_cookie_access() -> None:
    """Aucun endpoint privé, aucun cookie : uniquement le bouton natif."""
    code = _userscript_code()
    for forbidden in ("fetch(", "XMLHttpRequest", "cookie", "localStorage", "sessionStorage"):
        assert forbidden not in code, f"le userscript ne doit pas utiliser {forbidden!r}"


def test_userscript_uses_the_native_button_and_clicks_once() -> None:
    code = _userscript_code()
    assert 'data-testid="item-buy-button"' in code
    assert code.count("button.click(") == 1, "un seul clic natif, jamais de rafale"
    assert ".dispatchEvent(" not in code, "aucun événement synthétique"
    assert "setInterval" not in code, "aucune boucle de clic"


def test_userscript_visibility_does_not_depend_on_rendering() -> None:
    """Un onglet en arrière-plan rend des rectangles nuls : ne pas s'y fier.

    La visibilité se lit sur les styles calculés (`display`, `visibility`,
    `opacity`), pas sur `getBoundingClientRect` — sinon un onglet ouvert en
    arrière-plan, où Chromium rend des tailles nulles, ferait refuser à tort le
    clic sur un bouton pourtant présent.
    """
    code = _userscript_code()
    assert "getBoundingClientRect" not in code
    assert "getComputedStyle" in code


def test_userscript_wait_is_bounded() -> None:
    declared = re.search(r"const WAIT_TIMEOUT_MS = (\d+);", _userscript_code())
    assert declared is not None
    assert 0 < int(declared.group(1)) <= 30_000


def test_userscript_consumes_the_marker_before_waiting() -> None:
    """Le marqueur est retiré dans la même passe : rechargement et retour arrière muets."""
    source = _userscript()
    assert "replaceState" in source
    assert source.index("env.consumeMarker()") < source.index("while (env.now() < deadline)")


# ---------------------------------------------------------------------------
# Le harnais Node exécute la logique pure du script
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent de la machine")
def test_userscript_pure_logic_passes_its_harness() -> None:
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.startswith("OK "), completed.stdout
