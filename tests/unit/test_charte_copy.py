"""Verrous de charte sur les écrans qui n'étaient pas encore repris.

Ces tests ne vérifient pas une formulation : ils vérifient qu'un libellé ne peut
pas dériver d'un écran à l'autre, et qu'une donnée absente ne se déguise pas en
valeur. Un écran qui réécrit « Mots-clés exclus » ou « Non renseigné » en dur les
fait échouer ici, ce qui est exactement le but.
"""

import inspect
import pathlib
import uuid

import pytest

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
    # L'indication suit la même règle, et dit pourquoi elle n'est pas une information.
    assert "hors de la liste des informations" in docstring


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


def test_the_two_warnings_share_one_block_above_the_information_list():
    """Règle 3 : les avertissements sont en italique, **groupés** en tête de carte.

    Ils ont d'abord vécu à deux endroits — l'identité au-dessus de l'ISBN, le moteur tout
    en bas sous les critères — et l'esprit devait faire l'aller-retour entre le haut et
    le bas de la carte pour savoir ce qui manquait. Côte à côte, un seul style, une seule
    place : le bloc de notes se lit d'un coup, et c'est lui qui ouvre la carte.
    """
    import html

    from app.bot.handlers.alerts_helpers import RECAP_TITLE, format_semi_auto_recap

    isbn_note = "⚠️ « L'Âge d'or » : 3 autres ouvrages portent ce titre."
    keyword_note = "⚠️ L'IA n'a trouvé aucune exclusion pertinente pour cet ouvrage."
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
    assert lines[0] == RECAP_TITLE
    assert lines[1] == ""
    # Les deux avertissements se suivent : aucune ligne vide ne les sépare.
    assert lines[2] == escaped_isbn_note
    assert lines[3] == escaped_keyword_note
    # Une seule ligne vide sépare le bloc de notes de la liste des informations…
    assert lines[4] == ""
    assert lines[5].startswith("<b>ISBN :</b>")
    # … et la note du moteur n'est plus reléguée sous les critères qu'elle commente.
    assert recap.count(escaped_keyword_note) == 1
    price_index = next(
        index for index, line in enumerate(lines) if line.startswith("<b>Prix maximum :</b>")
    )
    assert lines.index(escaped_keyword_note) < price_index


def test_the_information_list_has_no_blank_line_before_the_max_price():
    """L'identité et les critères de veille se suivent : une seule liste, d'un trait.

    Le blanc qui séparait « Auteur » de « Prix maximum » coupait la fiche en deux blocs
    et laissait croire que le prix n'appartenait pas aux mêmes informations que l'auteur.
    La carte se lit maintenant de haut en bas, sans fausse frontière — et l'écran
    « Alerte créée », qui lit la même construction, en hérite sans rien demander.
    """
    from app.bot.handlers.alerts_helpers import format_semi_auto_recap

    recap = format_semi_auto_recap(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        isbn="9782205089752",
        max_price_cents=1200,
        include_keywords=["la couleur des choses"],
        exclude_keywords=[],
    )
    lines = recap.splitlines()

    author_index = next(
        index for index, line in enumerate(lines) if line.startswith("<b>Auteur :</b>")
    )
    price_index = next(
        index for index, line in enumerate(lines) if line.startswith("<b>Prix maximum :</b>")
    )
    assert price_index == author_index + 2
    assert lines[author_index + 1].startswith("<b>Éditeur :</b>")


def test_an_unidentified_work_is_announced_first_and_its_holes_are_shown():
    """Personne ne connaît l'ouvrage : la carte le dit en tête, et montre les trous.

    Le cas réel — un livre sorti la semaine dernière, absent de tous les catalogues —
    affichait `Titre :` et rien d'autre : ni auteur, ni éditeur, ni ISBN, sans un mot
    pour l'expliquer. L'absence se lisait comme un oubli de la carte. Elle devient un
    avertissement en tête, et les lignes manquantes disent « Non renseigné ».
    """
    from app.bot.handlers.alerts_helpers import UNIDENTIFIED_WORK_NOTE, creation_card

    recap = creation_card(
        {
            "name": "Dailycieux",
            "category_slug": "bd_comics",
            "author": None,
            "publisher": None,
            "isbn": None,
            "isbn_note": None,
            "book_unidentified": True,
            "max_item_price_cents": 3000,
            "include_keywords": ["dailycieux"],
            "exclude_keywords": [],
        }
    )
    lines = recap.splitlines()

    assert lines[2] == f"<i>{UNIDENTIFIED_WORK_NOTE}</i>"
    assert "<b>ISBN :</b> introuvable" in lines
    assert "<b>Auteur :</b> Non renseigné" in lines
    assert "<b>Éditeur :</b> Non renseigné" in lines
    assert "<b>Titre :</b> Dailycieux" in lines


def test_a_product_never_claims_a_hole_it_could_not_have():
    """Hors livres, il n'y a rien à trouver : aucun trou n'est annoncé.

    Un produit n'a ni ISBN ni auteur : déclarer ses champs « Non renseigné » inventerait
    une attente que la carte n'a jamais eue.
    """
    from app.bot.handlers.alerts_helpers import creation_card

    recap = creation_card(
        {
            "name": "Raspberry Pi 4",
            "category_slug": "tech",
            "author": None,
            "publisher": None,
            "isbn": None,
            "max_item_price_cents": 5000,
            "include_keywords": ["raspberry", "pi", "4"],
            "exclude_keywords": [],
        }
    )

    assert "Non renseigné" not in recap
    assert "<b>ISBN :</b>" not in recap
    assert "<b>Auteur :</b>" not in recap


def test_series_indication_lives_outside_the_information_list():
    """Règle 3, cas de l'indication : une série se dit hors de la liste des informations.

    La phrase était rendue en romain, coincée entre `Titre :` et `Auteur :` — elle se
    lisait comme une donnée de l'ouvrage. Elle est désormais une indication : italique,
    isolée par une ligne vide, au-dessus de la première ligne de champ. La ligne ISBN,
    elle, a disparu pour une série : `non utilisé — série multi-tomes` répétait la même
    phrase une seconde fois, à la mauvaise place.
    """
    from app.bot.handlers.alerts_helpers import (
        RECAP_TITLE,
        SERIES_INDICATION,
        format_semi_auto_recap,
    )

    recap = format_semi_auto_recap(
        title="Manuel du cuisinier amateur",
        author="Whoogy's",
        publisher="Chêne",
        isbn=None,
        max_price_cents=4000,
        include_keywords=["Manuel du cuisinier amateur"],
        exclude_keywords=["coffret"],
        is_series=True,
    )
    lines = recap.splitlines()

    assert lines[0] == RECAP_TITLE
    assert lines[1] == ""
    assert lines[2] == f"<i>{SERIES_INDICATION}</i>"
    assert lines[3] == ""
    assert lines[4].startswith("<b>Titre :</b>")
    assert "<b>ISBN :</b>" not in recap
    # La phrase du produit, verrouillée mot pour mot : c'est la copie que l'utilisateur lit.
    assert SERIES_INDICATION == "Série multi-tomes (veille sur tous les tomes)"


def test_a_recap_that_is_not_a_series_stays_exactly_as_it_was():
    """Hors série, la carte ne change pas : aucune indication, et l'italique du pied seul."""
    from app.bot.handlers.alerts_helpers import format_semi_auto_recap

    recap = format_semi_auto_recap(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        isbn="9782205089752",
        max_price_cents=1200,
        include_keywords=["La couleur des choses"],
        exclude_keywords=[],
    )

    assert "série" not in recap.lower()
    assert "<b>ISBN :</b> <code>9782205089752</code>" in recap
    # Seule la consigne finale porte l'italique.
    assert recap.count("<i>") == 1


# ---------------------------------------------------------------------------
# Libellés de charte : labels.py est la source unique, aucun écran ne les recopie
# ---------------------------------------------------------------------------


# Les écrans dont la charte relit les littéraux : handlers, fiche d'alerte, claviers
# et légende d'annonce. Le formatter et les claviers échappaient au verrou, qui ne
# lisait que `handlers` — `keyboards/alerts.py` réécrivait dès lors les sept libellés
# du menu d'édition, exactement la dérive que le verrou devait empêcher.
#
# `app/services/ai/pipeline.py` reste dehors à dessein : il compose un **prompt**
# pour le modèle (`Prix : …`), pas un écran que l'utilisateur lit — l'ajouter au
# périmètre y produirait un faux positif. Le brancher sur `FIELD_PRICE` serait pire
# encore : renommer un libellé d'IHM changerait alors le prompt sans toucher à sa
# version, donc sans changer la clé de cache d'analyse, et l'ancien résultat serait
# servi en silence pendant sept jours. Un prompt n'est pas un libellé d'affichage.
_SCREEN_SOURCES = (
    pathlib.Path("app/bot/handlers"),
    pathlib.Path("app/bot/formatter.py"),
    pathlib.Path("app/bot/keyboards"),
    pathlib.Path("app/services/telegram_service.py"),
)


def _screen_text_literals() -> list[tuple[str, int, str]]:
    """Littéraux de texte rendus par les écrans, docstrings exclues.

    Un libellé cité dans une docstring ne s'affiche nulle part : le verrou doit
    porter sur ce que l'utilisateur lit, pas sur le source entier. Les balises
    sont retirées avant comparaison pour que `<b>ISBN :</b>` se lise « ISBN : ».
    """
    import ast
    import re

    paths: list[pathlib.Path] = []
    for source in _SCREEN_SOURCES:
        paths.extend(sorted(source.rglob("*.py")) if source.is_dir() else [source])

    literals: list[tuple[str, int, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                literals.append(
                    (str(path.as_posix()), node.lineno, re.sub(r"<[^>]+>", "", node.value))
                )
    return literals


def test_no_charte_label_is_hardcoded_in_a_screen():
    """Un libellé de charte vient de `labels` : aucun écran ne le recopie en dur.

    Deux façons de dériver, une seule règle de détection : le libellé **tel quel**
    (un bouton du clavier d'édition, un titre d'écran de saisie) ou le libellé suivi
    de son séparateur (« ISBN : », « Statut : ») dans une ligne rendue. Le verrou
    lit les libellés du module — `FIELD_*`, `STATUS_*` et `BTN_*`, les statuts puis
    les boutons s'étant révélés réécrits en dur au même endroit — et non une liste
    recopiée : un libellé ajouté à `labels.py` est protégé dès sa naissance, et un
    écran qui réécrirait « Mots-clés exclus » échoue ici au lieu de dériver
    silencieusement.

    Un bouton n'a ni séparateur ni casse imposée : sa seule forme est son texte
    exact, et c'est aussi par lui qu'il dérive. « Retour » écrit à plusieurs
    endroits ne se répare qu'à plusieurs endroits — le verrou lui rend une source
    unique.

    Le périmètre est **tous** les écrans, pas les handlers seuls : la fiche, les
    claviers et la légende d'annonce portent les mêmes libellés, et un verrou qui
    n'en couvre qu'une partie laisse les autres recopier en les croyant protégés.
    """
    import re

    import app.bot.labels as labels

    charte_labels = {
        name: value
        for name, value in vars(labels).items()
        if name.startswith(("FIELD_", "STATUS_", "BTN_")) and isinstance(value, str)
    }
    assert charte_labels, (
        "labels n'expose aucun FIELD_/STATUS_/BTN_ : le verrou ne verrouille rien"
    )

    violations = []
    for file_name, line, text in _screen_text_literals():
        for name, label in charte_labels.items():
            if text.strip() == label or re.search(re.escape(label) + r"\s*:", text):
                violations.append(
                    f"{file_name}:{line} — {name} {label!r} écrit en dur : {text.strip()[:60]!r}"
                )

    assert not violations, (
        "Libellé(s) de charte recopié(s) en dur dans un écran — "
        "importer depuis app.bot.labels :\n" + "\n".join(violations)
    )


def _inline_button_texts() -> list[tuple[str, int, str]]:
    """Texte de chaque bouton inline de l'application, charte résolue.

    Une seule collecte pour les trois façons d'écrire le même bouton : le
    littéral (`text="Fermer"`), la constante de charte (`text=BTN_BACK`) et la
    f-string (`text=f"… {BTN_CANCEL}"`). On lit le sous-arbre `text=` de chaque
    `InlineKeyboardButton` et on y résout les noms contre `labels` — sans quoi
    réintroduire une flèche dans `BTN_BACK` échapperait au verrou, qui ne verrait
    qu'un nom.

    Le périmètre est toute l'application, pas les écrans : la règle ne vise pas un
    écran mais un bouton, et `app/tasks/proactive_autotune.py` en envoie aussi.
    """
    import ast

    import app.bot.labels as labels

    rows: list[tuple[str, int, str]] = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "InlineKeyboardButton"
            ):
                continue
            text = {kw.arg: kw.value for kw in node.keywords}.get("text")
            if text is None:
                continue
            for child in ast.walk(text):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    rows.append((path.as_posix(), node.lineno, child.value))
                elif isinstance(child, ast.Name):
                    value = getattr(labels, child.id, None)
                    if isinstance(value, str):
                        rows.append((path.as_posix(), node.lineno, value))
    return rows


def test_no_button_carries_an_arrow():
    """Une flèche ne sert à rien dans un bouton : le verrou l'interdit à la source.

    Règle 4 de la charte. Les deux façons d'en faire entrer une sont couvertes —
    l'écrire dans la constante, ou l'écrire dans le bouton — parce qu'elles se
    réparent séparément : retirer `←` de `BTN_BACK` laisserait les littéraux en
    place, et corriger les littéraux laisserait la constante remettre la flèche
    au clavier suivant.

    Une flèche de titre (`Détection → alerte`) décrit un flux, pas une action, et
    n'est pas un bouton : elle sort du périmètre puisqu'on ne lit que les
    `InlineKeyboardButton`, jamais le texte des messages.
    """
    import app.bot.labels as labels

    arrows = "←→↑↓"
    violations: list[str] = []

    for name, value in sorted(vars(labels).items()):
        if name.startswith("BTN_") and isinstance(value, str) and any(
            arrow in value for arrow in arrows
        ):
            violations.append(f"app/bot/labels.py — {name} {value!r} porte une flèche")

    seen: set[tuple[str, int, str]] = set()
    for file_name, line, text in _inline_button_texts():
        if not any(arrow in text for arrow in arrows):
            continue
        if (file_name, line, text) in seen:
            continue
        seen.add((file_name, line, text))
        violations.append(f"{file_name}:{line} — bouton fléché {text.strip()[:60]!r}")

    assert not violations, (
        "Flèche(s) dans un bouton — une flèche ne sert à rien, la retirer :\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Récap et confirmation : une seule carte, donc aucun des deux écrans ne peut
# taire un cas que l'autre affiche — la ligne d'édition, l'identifiant introuvable.
# ---------------------------------------------------------------------------


def _creation_draft(**overrides):
    """Brouillon minimal, avec des valeurs neutres sauf celles fournies."""
    draft = {
        "name": "La couleur des choses",
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "isbn": None,
        "isbn_note": None,
        "is_series": False,
        "category_slug": "bd_comics",
        "edition_alternatives": None,
        "max_item_price_cents": 1200,
        "selected_conditions": ["Très bon état"],
        "include_keywords": ["la couleur des choses"],
        "exclude_keywords": [],
        "variants": None,
        "keyword_note": None,
    }
    draft.update(overrides)
    return draft


def test_the_confirmation_is_the_recap_reread_from_the_same_construction():
    """Un seul texte pour les deux écrans : en-tête, pied et bloc de cote exceptés.

    La confirmation écrivait ses lignes à la main, donc elle avait laissé derrière elle
    la ligne d'édition et l'identifiant introuvable — deux cas que le récap affichait et
    qu'elle taisait. Le corps est désormais le même objet ; le verrou compare le corps
    entier, pas une ligne, pour qu'une information ajoutée à un écran s'ajoute à l'autre.
    """
    from app.bot.handlers.alerts_create import _CONFIRMATION_FOOTER
    from app.bot.handlers.alerts_helpers import (
        ALERT_CREATED_TITLE,
        RECAP_FOOTER,
        RECAP_TITLE,
        creation_card,
    )
    from app.services.market_price_service import PENDING_COTE_LINE

    draft = _creation_draft(
        isbn="9782369903086",
        edition_alternatives=[
            {
                "isbn13": "9782369903086",
                "publication_year": 2022,
                "publisher": "Çà et là",
                "format_detail": "1 vol. (224 p.) ; 24 cm",
            },
            {
                "isbn13": "9782369904151",
                "publisher": "Éditions poches",
                "format_detail": "1 vol. (224 p.) ; 19 cm",
            },
        ],
    )

    recap = creation_card(draft)
    confirmation = creation_card(
        draft,
        header=ALERT_CREATED_TITLE,
        footer=_CONFIRMATION_FOOTER,
        extra_lines=[PENDING_COTE_LINE],
    )

    recap_lines = recap.splitlines()
    confirm_lines = confirmation.splitlines()
    assert recap_lines[0] == RECAP_TITLE
    assert confirm_lines[0] == ALERT_CREATED_TITLE
    # Le récap ferme sur ["", pied] ; la confirmation sur ["", cote, "", pied].
    assert recap_lines[2:-2] == confirm_lines[2:-4]
    # La ligne d'édition, que la confirmation avait perdue, est dans le corps partagé.
    assert "<b>Édition :</b> 2022 · Çà et là · 224 p. · 24 cm" in recap_lines
    # Le bloc de cote n'appartient qu'à la confirmation.
    assert confirm_lines[-3] == PENDING_COTE_LINE
    assert PENDING_COTE_LINE not in recap
    assert recap_lines[-1] == RECAP_FOOTER
    assert confirm_lines[-1] == _CONFIRMATION_FOOTER


def test_the_unfound_identifier_is_announced_by_both_screens():
    """« introuvable » est une information : aucun des deux écrans ne peut la taire.

    La ligne ne nomme plus d'autorité : la BNF n'est plus la seule à avoir été
    interrogée (ADR-033), et le *pourquoi* vit désormais dans l'avertissement de tête.
    """
    from app.bot.handlers.alerts_create import _CONFIRMATION_FOOTER
    from app.bot.handlers.alerts_helpers import ALERT_CREATED_TITLE, creation_card

    draft = _creation_draft(isbn=None)
    line = "<b>ISBN :</b> introuvable"

    assert line in creation_card(draft)
    assert line in creation_card(
        draft, header=ALERT_CREATED_TITLE, footer=_CONFIRMATION_FOOTER
    )


def test_a_card_without_an_identifier_stays_silent_on_both_screens():
    """Un produit n'a pas d'identifiant à trouver : les deux écrans se taisent pareil."""
    from app.bot.handlers.alerts_create import _CONFIRMATION_FOOTER
    from app.bot.handlers.alerts_helpers import ALERT_CREATED_TITLE, creation_card

    draft = _creation_draft(
        isbn=None, category_slug="tech", author=None, publisher=None
    )

    for card in (
        creation_card(draft),
        creation_card(draft, header=ALERT_CREATED_TITLE, footer=_CONFIRMATION_FOOTER),
    ):
        assert "<b>ISBN :</b>" not in card


# ---------------------------------------------------------------------------
# Ecran « prix maximum » : il lit la carte, il n'ecrit plus ses lignes
# d'identite a la main.
# ---------------------------------------------------------------------------


async def _price_screen_text(kind: str) -> tuple[str, dict]:
    """Joue une recherche jusqu'a l'ecran de prix, rend `(texte envoye, brouillon)`.

    Le brouillon est relu par l'ecran lui-meme (`state.get_data`), pas reconstruit ici :
    ce que le test compare est exactement ce que l'utilisateur liera. La recherche BD
    publie d'abord un message d'etat puis l'edite ; un produit repond directement.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import Message

    from app.bot.handlers.alerts_helpers import (
        process_bd_query_lookup,
        process_tech_or_clothes_query,
    )
    from app.services.book_metadata_service import BookMetadata, BookResolution

    state_data: dict = {}

    async def _update_data(**kwargs):
        state_data.update(kwargs)

    async def _get_data() -> dict:
        return dict(state_data)

    state = AsyncMock()
    state.update_data = _update_data
    state.get_data = _get_data
    state.set_state = AsyncMock()

    message = MagicMock(spec=Message)
    status = MagicMock(spec=Message)
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)

    category_repo = MagicMock()
    category_repo.get_by_slug = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    def _record(call: "AsyncMock") -> str:
        call.assert_called_once()
        return call.call_args[0][0]

    if kind == "bd":
        meta = BookMetadata(
            title="La couleur des choses",
            author="Martin Panchaud",
            publisher="Çà et là",
            isbn13="9782205089752",
            recommended_includes=["la couleur des choses"],
            recommended_excludes=["coffret"],
            recommended_variants=[],
        )
        service = MagicMock()
        service.resolve_input = AsyncMock(return_value=BookResolution(best=meta))
        with (
            patch(
                "app.bot.handlers.alerts_helpers.BookMetadataService", return_value=service
            ),
            patch("app.bot.handlers.alerts.CategoryRepository", return_value=category_repo),
        ):
            await process_bd_query_lookup(message, state, "la couleur des choses", AsyncMock())
        text = _record(status.edit_text)
    else:
        with patch("app.bot.handlers.alerts.CategoryRepository", return_value=category_repo):
            await process_tech_or_clothes_query(
                message, state, "Raspberry Pi 4", "tech", AsyncMock()
            )
        text = _record(message.answer)

    return text, dict(state_data)


@pytest.mark.asyncio
async def test_the_price_screen_reads_the_creation_card():
    """L'ecran de prix est la carte de creation : il ne construit plus ses lignes.

    Il ecrivait `<b>Auteur :</b>`, `<b>ISBN :</b>` et les criteres a la main, avec ses
    propres emojis et ses propres libelles (`Inclus`, `Exclus`, `Etats`) : le libelle
    d'un champ pouvait donc diverger de celui du recap qui suivait, un ecran plus bas.
    Le verrou compare le rendu entier a la carte, pas une ligne — une information
    ajoutée a `creation_card` apparait ici, et un ecran reintroduit echoue.
    """
    from app.bot.handlers.alerts_helpers import PRICE_PROMPT_FOOTER, creation_card

    rendered, draft = await _price_screen_text("bd")

    assert rendered == creation_card(draft, footer=PRICE_PROMPT_FOOTER)
    # Les lignes d'identite ecrites a la main ont disparu au profit de la carte.
    assert "✍️" not in rendered
    assert "🔢" not in rendered
    assert "🔑 <b>Inclus :</b>" not in rendered
    assert "<b>Auteur :</b> Martin Panchaud" in rendered


@pytest.mark.asyncio
async def test_the_price_screen_of_a_product_reads_the_same_card():
    """Un produit n'a pas de ligne d'ISBN, et son prix vient de la meme carte."""
    from app.bot.handlers.alerts_helpers import PRICE_PROMPT_FOOTER, creation_card

    rendered, draft = await _price_screen_text("tech")

    assert rendered == creation_card(draft, footer=PRICE_PROMPT_FOOTER)
    assert "<b>ISBN :</b>" not in rendered
    assert "🎯" not in rendered
