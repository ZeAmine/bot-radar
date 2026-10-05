"""Verrous de charte sur les écrans qui n'étaient pas encore repris.

Ces tests ne vérifient pas une formulation : ils vérifient qu'un libellé ne peut
pas dériver d'un écran à l'autre, et qu'une donnée absente ne se déguise pas en
valeur. Un écran qui réécrit « Mots-clés exclus » ou « Non renseigné » en dur les
fait échouer ici, ce qui est exactement le but.
"""

import inspect
import uuid

from app.bot.handlers.autotune import _autotune_back_keyboard, _autotune_header
from app.bot.handlers.help import HELP_TEXT
from app.bot.keyboards.stats import get_stats_keyboard
from app.bot.labels import (
    BTN_BACK_TO_AUTOTUNE,
    BTN_CANCEL,
    BTN_CONFIRM,
    BTN_EDIT_ALERT_SETTINGS,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_NAME,
    NOT_PROVIDED,
    TITLE_AUTOTUNE,
    TITLE_EDIT_EXCLUDES,
    TITLE_HELP,
    TITLE_OFFER,
    TITLE_STATS,
    format_price_line,
    period_button,
)
from app.services.ai.schemas import NegotiationResult
from app.services.market_price_service import MarketPriceService
from app.services.price_calculator import format_price_summary


def test_help_screen_leads_with_the_charte_title():
    """`/help` commence par le titre de charte, pas par un préambule."""
    assert HELP_TEXT.startswith(TITLE_HELP)


def test_help_lists_every_registered_command():
    """Chaque commande annoncée dans le menu Telegram figure dans `/help`.

    Une commande offerte dans la barre de commandes mais absente de l'aide est
    une commande que l'utilisateur doit connaître par hasard.
    """
    for command in (
        "/add",
        "/show",
        "/list",
        "/edit",
        "/pause",
        "/resume",
        "/delete",
        "/categories",
        "/stats",
        "/autotune",
        "/status",
        "/cancel",
        "/help",
    ):
        assert command in HELP_TEXT, command


def test_help_never_exclaims():
    """La charte interdit l'exclamation dans la rédaction utilisateur."""
    assert "!" not in HELP_TEXT


def test_period_button_marks_only_the_current_period():
    """Une période cochée, l'autre neutre : deux boutons ne doivent pas se confondre."""
    assert period_button("7 jours", True) == "✅ 7 jours"
    assert period_button("7 jours", False) == "7 jours"


def test_stats_keyboard_shows_exactly_one_checked_period():
    """Le clavier `/stats` coche la période affichée, et elle seule."""
    for current, checked in ((7, 0), (30, 1)):
        keyboard = get_stats_keyboard(current_days=current).inline_keyboard[0]
        marked = [index for index, button in enumerate(keyboard) if "✅" in button.text]
        assert marked == [checked], current


def test_stats_title_comes_from_labels():
    """Le titre `/stats` n'est écrit qu'à un seul endroit."""
    assert TITLE_STATS.startswith("📊")


def test_autotune_header_uses_the_shared_field_labels():
    """L'en-tête auto-tuning ne réinvente ni son titre ni son libellé de champ."""
    header = _autotune_header("Watchmen")

    assert header.startswith(TITLE_AUTOTUNE)
    assert f"{FIELD_NAME} : « Watchmen »" in header


def test_autotune_header_escapes_the_alert_name():
    """Un nom d'alerte contenant du HTML ne peut pas casser la mise en forme."""
    header = _autotune_header("<b>BD</b> & cie")

    assert "<b>BD</b>" not in header
    assert "&lt;b&gt;BD&lt;/b&gt; &amp; cie" in header


def test_autotune_back_keyboard_labels_are_shared():
    """Les deux boutons de sortie de l'auto-tuning viennent des libellés communs."""
    keyboard = _autotune_back_keyboard("abc").inline_keyboard

    assert keyboard[0][0].text == BTN_BACK_TO_AUTOTUNE
    assert keyboard[0][0].callback_data == "ai_tune_hub"
    assert keyboard[1][0].text == BTN_EDIT_ALERT_SETTINGS
    assert keyboard[1][0].callback_data == "al_edit:abc"


def test_negotiation_offer_uses_the_charter_price_format():
    """Le coach propose un montant au format de la charte, pas « 25 € »."""
    from app.services.ai.modules.negotiation import NegotiationModule

    rendered = NegotiationModule().format_telegram_html(
        NegotiationResult(suggested_offer_cents=2500, suggested_offer_euros=25.0, message="Bonjour")
    )

    assert rendered.startswith(TITLE_OFFER)
    assert "25,00 €" in rendered
    assert "<i>Touche le texte ci-dessous pour le copier</i>" in rendered


def test_exclusion_edit_screen_uses_its_own_title():
    """L'écran de retouche des exclusions a un titre à lui, pas celui de l'alerte."""
    assert TITLE_EDIT_EXCLUDES.startswith("✏️")


def test_absent_exclusions_read_as_not_provided_not_zero():
    """« Aucune » n'est pas une valeur : l'absence se dit « Non renseigné »."""
    assert NOT_PROVIDED == "Non renseigné"
    assert FIELD_EXCLUDE_KEYWORDS == "Mots-clés exclus"


# ---------------------------------------------------------------------------
# Ligne de prix : regle de charte, valable pour toutes les alertes a venir
# ---------------------------------------------------------------------------


def test_price_line_puts_the_total_in_parentheses():
    """« **Prix :** 4,00 € (7,78 € TTC) » : le format demandé, verrouillé.

    Le libellé du total tient en trois lettres. « Total est. » était treize
    caractères pour dire la même chose, et « estimé » se lisait comme un doute
    sur un montant — protection et port sont, eux, parfaitement réels.

    Le libellé vit dans `labels`, pas dans chaque appelant : une future alerte
    qui afficherait un total héritera du format sans le redemander.
    """
    assert format_price_line(400, 778) == ("<b>Prix :</b> 4,00 € (7,78 € TTC)")


def test_price_line_omits_an_unknown_total_rather_than_faking_one():
    """Sans total connu, la parenthese disparait au lieu de laisser un trou."""
    assert format_price_line(400, None) == "<b>Prix :</b> 4,00 €"


def test_price_line_uses_the_french_decimal_separator():
    """La virgule decimale, comme tous les autres montants du bot."""
    assert format_price_line(400, 778).count(",") == 2


def test_price_summary_delegates_to_the_charte():
    """Le calculateur calcule, la charte rend : un seul endroit ecrit la ligne."""
    assert format_price_summary(item_price_cents=400, estimated_total_cents=778) == (
        "<b>Prix :</b> 4,00 € (7,78 € TTC)"
    )


def test_price_calculations_are_untouched():
    """Le format a change, pas les frais : 0,70 € + 5% + 2,88 € restent exacts."""
    from app.services.price_calculator import (
        calculate_buyer_protection_cents,
        calculate_estimated_total,
    )

    assert calculate_buyer_protection_cents(400) == 90
    assert calculate_estimated_total(400, fallback_defaults=True) == 778


# ---------------------------------------------------------------------------
# Cote de marche : la ligne nomme sa source, elle ne se justifie pas
# ---------------------------------------------------------------------------


def _rendered_market_strings() -> list[str]:
    """Chaînes que le module renvoie réellement, commentaires et docstrings exclus.

    Un emoji cité dans un commentaire ne s'affiche nulle part : le verrou doit
    porter sur ce que l'utilisateur lit, pas sur le source entier.
    """
    import ast

    tree = ast.parse(inspect.getsource(MarketPriceService))
    rendered: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            rendered.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            rendered.extend(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
    return rendered


def test_market_price_lines_carry_no_decorative_emoji():
    """📉 et 📊 disparaissent des lignes de prix : le libelle porte le sens.

    Verrou global plutôt qu'une ligne par écran : un rendu ajouté plus tard
    réintroduirait l'emoji quelque part, et ce test le verrait.
    """
    for text in _rendered_market_strings():
        assert "\U0001f4c9" not in text, text
        assert "📊 Prix du marché" not in text, text


def test_market_panel_does_not_explain_the_reference_price_in_parentheses():
    """« Cote d'occasion basee sur le plus bas prix... » ne revient pas au panneau."""
    for text in _rendered_market_strings():
        assert "nous avons pu constater" not in text, text
        assert "Cote d'occasion basée" not in text, text


# ---------------------------------------------------------------------------
# Bouton de confirmation : il ne repete pas ce que l'ecran vient d'annoncer
# ---------------------------------------------------------------------------


def test_dislike_confirmation_button_is_short():
    """« Confirmer » suffit : le message au-dessus dit deja ce qui est confirme."""
    from app.bot.keyboards.listings import get_dislike_confirmation_keyboard
    from app.core.constants import CategorySlug

    keyboard = get_dislike_confirmation_keyboard(
        listing_id=uuid.uuid4(),
        listing_url="https://www.vinted.fr/item/1",
        category_slug=CategorySlug.BD_COMICS.value,
    )
    confirm, cancel = keyboard.inline_keyboard[-1]

    assert confirm.text == BTN_CONFIRM == "Confirmer"
    assert cancel.text == BTN_CANCEL


# ---------------------------------------------------------------------------
# Charte de rendu (ADR-022) : libellé gras / valeur romaine, consigne en
# italique, avertissement italique et isolé. Trois pertes invisibles aux autres
# tests parce qu'elles ne se voient qu'à l'écran — donc trois verrous ici.
# ---------------------------------------------------------------------------


def test_charte_documents_its_three_rendering_rules():
    """Les règles vivent dans `labels`, source unique : un écran ne les réinvente pas."""
    import app.bot.labels as labels

    docstring = labels.__doc__ or ""
    assert "libellé de champ est gras" in docstring
    assert "instruction, d'exemple ou d'aide est en italique" in docstring
    assert "avertissement est en italique et isolé" in docstring


def test_field_labels_are_bold_and_values_stay_roman():
    """`**Prix :** 4,00 €` : le libellé guide, la donnée reste en romain.

    Un gras qui engloberait la valeur noierait la lecture au lieu de la guider —
    c'est la règle 1, et elle se vérifie sur la ligne partagée par tous les écrans.
    """
    line = format_price_line(400, 778)

    assert "<b>Prix :</b>" in line
    assert "<b>4,00 €" not in line
    assert "<b>7,78 €" not in line
    # « TTC » qualifie le montant, ce n'est pas le libellé d'un champ : le gras
    # de « Prix : » ne déborde pas sur lui.
    assert "TTC" in line
    assert "<b>TTC</b>" not in line


def test_alert_caption_bolds_labels_and_leaves_values_roman():
    """L'alerte d'annonce suit la règle 1 : `État` et `Mots-clés` gras, valeurs nues."""
    import uuid as _uuid

    from app.core.constants import CategorySlug
    from app.db.models.listing import Listing
    from app.services.telegram_service import build_listing_caption

    listing = Listing(
        id=_uuid.uuid4(),
        source_name="authorized-source",
        external_id=str(_uuid.uuid4()),
        url="https://www.example.com/listing-1",
        title="Blacksad — Intégrale",
        normalized_title="blacksad — intégrale",
        category_slug=CategorySlug.BD_COMICS.value,
        item_price_cents=900,
        estimated_total_cents=1369,
        condition="Très bon état",
        raw_payload={},
    )
    caption = build_listing_caption(
        listing, market_comparison_line=None, alert_keywords=["blacksad"]
    )

    assert "<b>État :</b> TBE" in caption
    assert "<b>TBE</b>" not in caption
    assert "<b>Mots-clés :</b> blacksad" in caption
    assert "<b>blacksad</b>" not in caption


def test_instruction_text_is_italic():
    """Les consignes portent l'italique : une instruction n'est pas une donnée."""
    from app.bot.handlers.alerts_create import _CONFIRMATION_FOOTER
    from app.bot.handlers.alerts_helpers import format_semi_auto_recap

    recap = format_semi_auto_recap(
        title="La couleur des choses",
        author=None,
        publisher=None,
        isbn=None,
        max_price_cents=1200,
        include_keywords=["la couleur des choses"],
        exclude_keywords=[],
    )

    assert recap.rstrip().endswith("<i>Valide les critères ci-dessus avant de créer l'alerte.</i>")
    assert _CONFIRMATION_FOOTER.startswith("<i>")
    assert _CONFIRMATION_FOOTER.endswith("</i>")


def test_both_warnings_are_italic_and_the_identity_one_leads_above_the_isbn():
    """Règle 3 : les deux avertissements sont en italique, isolés, au-dessus de l'ISBN.

    L'avertissement d'identité était collé entre l'ISBN et le titre, donc invisible,
    et en romain tandis que la note du moteur était en italique. Deux avertissements,
    un seul style et une seule place : la référence.
    """
    import html

    from app.bot.handlers.alerts_helpers import RECAP_TITLE, format_semi_auto_recap

    isbn_note = "⚠️ « L'Âge d'or » : 3 autres ouvrages portent ce titre."
    keyword_note = "🔎 L'IA n'a trouvé aucune exclusion pertinente pour cet ouvrage."
    recap = format_semi_auto_recap(
        title="L'Âge d'or",
        author=None,
        publisher=None,
        isbn=None,
        max_price_cents=1500,
        include_keywords=["L'Âge d'or"],
        exclude_keywords=[],
        isbn_note=isbn_note,
        keyword_note=keyword_note,
    )

    # `isbn_note` porte des données de notice, donc échappé ; `keyword_note` est un
    # libellé interne, rendu tel quel.
    escaped_isbn_note = f"<i>{html.escape(isbn_note)}</i>"
    escaped_keyword_note = f"<i>{keyword_note}</i>"

    lines = recap.splitlines()
    assert escaped_isbn_note in lines
    assert escaped_keyword_note in lines
    # L'avertissement d'identité précède l'ISBN, et rien ne le colle à lui.
    note_index = lines.index(escaped_isbn_note)
    isbn_index = next(i for i, line in enumerate(lines) if line.startswith("<b>ISBN :</b>"))
    assert note_index < isbn_index
    assert lines[0] == RECAP_TITLE
    assert lines[note_index - 1] == ""
    assert lines[note_index + 1] == ""
    # La note du moteur est isolée des critères qu'elle commente.
    keyword_index = lines.index(escaped_keyword_note)
    assert lines[keyword_index - 1] == ""
