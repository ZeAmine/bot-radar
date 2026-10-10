"""Verrous de charte : libellés partagés, et copie observée sur les écrans de création.

Deux natures de test, et deux seulement.

- Des verrous de **source** : un libellé de charte vit dans `labels` et nulle part
  ailleurs, la cote ne porte pas d'emoji décoratif. Ils lisent le code, parce que ce
  qu'ils protègent est justement qu'aucun écran ne réécrive ces textes.
- Des verrous de **copie observée** : ce que l'utilisateur lit, obtenu en traversant les
  **vrais handlers** — ou le producteur réel quand il n'y a pas de handler, comme la
  légende d'annonce, qui naît à l'ingestion. Les simulations viennent de
  `tests/add_journey.py`, jamais recopiées ici ; un changement de copie qui n'atteint pas
  l'écran réel ne peut pas passer.

Les mêmes écrans sont relus ligne à ligne par les parcours de `test_add_screens` — les
deux avertissements en tête et côte à côte, le ⚠️ à la place de la loupe, l'absence de
blanc avant « Prix maximum », les trous d'identité quand personne n'identifie l'ouvrage.
Les verrous qui rejouaient ces cas sur une fonction de rendu appelée à la main ont été
supprimés plutôt que dupliqués : la couverture est la même, elle passe par le parcours.
"""

import inspect
import pathlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.alerts_create import process_max_price_input, save_alert, start_add_alert
from app.bot.handlers.alerts_helpers import RECAP_TITLE
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
from app.db.models.user import User
from app.services.ai.schemas import NegotiationResult
from app.services.book_metadata_service import BibliographicReport, BookResolution
from app.services.market_price_service import PENDING_COTE_LINE, MarketPriceService
from app.services.price_calculator import format_price_summary
from app.services.telegram_service import build_listing_caption
from app.use_cases.book_search import result_from_report

# Le harnais de parcours : les écrans de copie sont joués par les vrais handlers, jamais
# par un appel direct à `creation_card`. Les simulations sont partagées — doubles dans
# `tests/add_fakes.py`, câblage dans `tests/add_journey.py`, écrans dans `tests/screens.py`
# — et jamais recopiées ici : deux copies donneraient deux chaînes légèrement
# différentes, donc deux vérités sur ce que l'utilisateur lit.
from tests.add_fakes import (
    BNF_ALTERNATIVE,
    BNF_EDITION,
    DraftState,
    deepseek_payload,
    fake_bnf,
    fake_deepseek,
)
from tests.add_journey import defer_task, drain, patched, repositories
from tests.screens import confirm_press, incoming_message, message_with_recap
from tests.telegram_fakes import telegram_message


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


@pytest.mark.asyncio
async def test_the_alert_sent_to_telegram_bolds_labels_and_leaves_values_roman():
    """L'alerte d'annonce suit la règle 1 : `État` et `Mots-clés` gras, valeurs nues.

    La légende est relue **telle qu'elle part** : `send_listing_alert` est son seul
    producteur — le chemin d'ingestion — et c'est le texte qu'il remet à Telegram qui
    est comparé ici. Aucun handler n'en rend (le verrou suivant le tient), donc il n'y a
    pas de chaîne d'écran à traverser ; la copie n'est pas pour autant jugée sur un
    rendu appelé à la main.
    """
    from app.core.constants import CategorySlug
    from app.db.models.listing import Listing
    from app.services.telegram_service import send_listing_alert

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=telegram_message(12345, 1))
    listing = Listing(
        id=uuid.uuid4(),
        source_name="authorized-source",
        external_id=str(uuid.uuid4()),
        url="https://www.example.com/listing-1",
        title="Blacksad — Intégrale",
        normalized_title="blacksad — intégrale",
        category_slug=CategorySlug.BD_COMICS.value,
        item_price_cents=900,
        estimated_total_cents=1369,
        condition="Très bon état",
        raw_payload={},
        photos=[],
    )

    # L'enrichissement IA n'est pas le sujet de ce verrou : sa coroutine est seulement
    # retenue, puis refermée sans être jouée.
    postponed: list = []
    with patch("app.services.telegram_service.safe_create_task", new=defer_task(postponed)):
        sent = await send_listing_alert(bot, 12345, listing, alert_keywords=["blacksad"])
    for coro, _nom in postponed:
        coro.close()

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    caption = bot.send_message.call_args.kwargs["text"]
    assert "<b>État :</b> TBE" in caption
    assert "<b>TBE</b>" not in caption
    assert "<b>Mots-clés :</b> blacksad" in caption
    assert "<b>blacksad</b>" not in caption


def _caption_renders(root: pathlib.Path) -> list[str]:
    """Appels au rendu de la légende d'annonce écrits sous `root`, chemin:ligne.

    Un **appel**, pas une mention. Le nom est suivi jusque dans le source — sous son
    nom importé (alias compris), derrière son module (`telegram_service.…`), ou sous le
    nom qu'on lui a donné en l'important — et c'est l'invocation qui compte : un
    commentaire ou une docstring qui le nomment ne rendent rien. Lire le source par
    contenance de chaîne confondait les deux, dans un fichier dont tous les autres
    verrous lisent l'arbre syntaxique.

    Le nom cherché vient de la fonction elle-même, pas d'un littéral recopié : un
    renommage du rendu suit, au lieu de laisser le verrou surveiller un nom mort.
    """
    import ast

    renderer = build_listing_caption.__name__
    renders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Les noms sous lesquels ce module tient le rendu. Un `import … as ts` ne le
        # nomme pas ici : il le laisse derrière son module, que l'on regarde aussi.
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == renderer
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            if isinstance(called, ast.Name) and called.id in imported:
                renders.append(f"{path.as_posix()}:{node.lineno} — {called.id}()")
            elif isinstance(called, ast.Attribute) and called.attr == renderer:
                renders.append(
                    f"{path.as_posix()}:{node.lineno} — {ast.unparse(called)}()"
                )
    return renders


def test_no_screen_renders_the_listing_caption():
    """La légende d'annonce n'a qu'un rendu, et il n'est pas dans un écran.

    C'est la raison pour laquelle le verrou ci-dessus passe par `send_listing_alert` :
    il n'existe aucune chaîne de handler qui produise cette légende, et il ne doit pas
    en exister — un écran qui en rendrait une seconde la ferait diverger de celle qui
    part réellement sur Telegram, et c'est l'alerte qui porte la copie que l'utilisateur
    lit en premier.
    """
    renders = _caption_renders(pathlib.Path("app/bot"))

    assert not renders, (
        "La légende d'annonce est rendue par `send_listing_alert`, sur le chemin "
        "d'ingestion : aucun écran ne doit en écrire une seconde —\n" + "\n".join(renders)
    )


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
# Copie observee : l'ecran de prix et la confirmation, joues par leurs handlers
#
# Les deux ecrans lisent la meme carte de creation, et c'est ce que ces tests
# regardent — le **message envoye**, jamais le resultat d'une fonction de rendu
# appelee a la main.
# ---------------------------------------------------------------------------


async def _price_screen_text(kind: str) -> tuple[str, AsyncMock]:
    """Joue une recherche jusqu'a l'ecran de prix, rend `(texte envoye, etat)`.

    L'etat est relu par l'ecran lui-meme (`state.get_data`), pas reconstruit ici :
    les tests qui enchainent sur la saisie du prix rejouent donc exactement le
    parcours de l'utilisateur. La recherche BD publie d'abord un message d'etat puis
    l'edite ; un produit repond directement.
    """
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

    def _record(call: AsyncMock) -> str:
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
        service.search_by_title = AsyncMock(
            return_value=result_from_report(
                BibliographicReport(kind="title", resolution=BookResolution(best=meta)),
                query_title="la couleur des choses",
            )
        )
        with (
            patch(
                "app.bot.handlers.alerts_helpers.BookSearchUseCase", return_value=service
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

    return text, state


@pytest.mark.asyncio
async def test_the_price_screen_of_a_book_is_the_creation_card_without_the_price():
    """L'ecran de prix lit la carte : memes lignes, memes libelles, un seul manque.

    Il ecrivait `<b>Auteur :</b>`, `<b>ISBN :</b>` et les criteres a la main, avec ses
    propres emojis et ses propres libelles (`Inclus`, `Exclus`, `Etats`) : le libelle
    d'un champ pouvait donc diverger de celui du recap qui suivait, un ecran plus bas.
    Le montant est la seule piece manquante, et c'est la consigne du pied qui le dit.
    """
    rendered, _etat = await _price_screen_text("bd")
    lines = rendered.splitlines()

    assert rendered.startswith(f"{RECAP_TITLE}\n\n")
    assert "<b>ISBN :</b> <code>9782205089752</code>" in lines
    assert "<b>Titre :</b> La couleur des choses" in lines
    assert "<b>Auteur :</b> Martin Panchaud" in lines
    assert "<b>Éditeur :</b> Çà et là" in lines
    assert "<b>Mots-clés inclus :</b> couleur des choses" in lines
    assert "<b>Mots-clés exclus :</b> roman, poche, anglais, english, dvd" in lines
    assert lines[-1] == (
        "💶 <i>Indique ton prix maximum pour cette alerte (en €) (ou /cancel) :</i>"
    )
    # Hors serie et ouvrage identifie : la seule indication en italique est la consigne.
    assert "série" not in rendered.lower()
    assert rendered.count("<i>") == 1
    # Les lignes ecrites a la main, avec leurs emojis, ont disparu au profit de la carte.
    assert "✍️" not in rendered
    assert "🔢" not in rendered
    assert "🔑 <b>Inclus :</b>" not in rendered


@pytest.mark.asyncio
async def test_a_product_never_claims_a_hole_it_could_not_have():
    """Hors livres, il n'y a rien a trouver : aucun trou n'est annonce.

    Un produit n'a ni ISBN ni auteur. Les deux ecrans du parcours — la demande du prix,
    puis le recap qui suit la saisie du montant — pourraient inventer cette attente en
    ecrivant « Non renseigne » sous des champs qui n'ont jamais existe. Le prix, lui,
    est reellement manquant tant qu'il n'est pas saisi : c'est la seule absence que la
    carte a le droit de montrer.
    """
    ecran_prix, etat = await _price_screen_text("tech")

    assert "<b>Titre :</b> Raspberry Pi 4" in ecran_prix
    assert "<b>ISBN :</b>" not in ecran_prix
    assert "<b>Auteur :</b>" not in ecran_prix

    saisie = incoming_message("50")
    await process_max_price_input(saisie, etat, AsyncMock())
    recap = saisie.answer.call_args[0][0]

    assert "<b>Prix maximum :</b> 50,00 €" in recap
    assert "Non renseigné" not in recap
    assert "<b>ISBN :</b>" not in recap
    assert "<b>Auteur :</b>" not in recap


@pytest.mark.asyncio
async def test_the_confirmation_is_the_recap_read_back_from_the_same_card():
    """Une seule carte pour les deux ecrans : le corps passe de l'un a l'autre entier.

    La confirmation ecrivait ses lignes a la main, donc elle avait laisse derriere elle
    la ligne d'edition et l'identifiant introuvable — deux cas que le recap affichait et
    qu'elle taisait. Le verrou ne nomme aucune de ces lignes : il exige la **liste
    entiere** des informations du recap, dans le meme ordre, dans la confirmation. Une
    information ajoutee a un ecran s'ajoute a l'autre, et aucune ne peut manquer d'un
    cote sans faire echouer ce test.
    """
    resolution = BookResolution(best=BNF_EDITION, alternatives=[BNF_ALTERNATIVE])
    repos = repositories(BNF_EDITION.isbn13)
    message, loading_message = message_with_recap("/add la couleur des choses. Max 12 €")
    state = DraftState()

    patches = patched(repos, fake_bnf(resolution), fake_deepseek(deepseek_payload()))
    for entered in patches:
        entered.start()
    try:
        await start_add_alert(message, state)
        await drain(repos["tasks"])
    finally:
        for entered in reversed(patches):
            entered.stop()

    recap = loading_message.edit_text.call_args_list[-1][0][0]

    callback, confirm_message = confirm_press()
    confirm_repos = repositories(BNF_EDITION.isbn13)
    patches = patched(confirm_repos, MagicMock(), MagicMock())
    for entered in patches:
        entered.start()
    try:
        await save_alert(
            callback, state, User(id=uuid.uuid4(), telegram_user_id=123), AsyncMock()
        )
        await drain(confirm_repos["tasks"])
    finally:
        for entered in reversed(patches):
            entered.stop()

    confirmation = confirm_message.edit_text.call_args[0][0]
    recap_lines = recap.splitlines()
    confirm_lines = confirmation.splitlines()

    assert recap_lines[0] == RECAP_TITLE
    assert confirm_lines[0] == "✅ <b>Alerte créée</b>"
    # Le corps, ligne de champ par ligne de champ : le meme, dans le meme ordre.
    assert [line for line in recap_lines if line.startswith("<b>")] == [
        line for line in confirm_lines if line.startswith("<b>")
    ]
    # Le bloc de cote n'appartient qu'a la confirmation : c'est ce que l'ecran vient de
    # produire, pas un critere de veille.
    assert PENDING_COTE_LINE not in recap
    assert "<i>⚠️ Cote indisponible</i>" in confirmation
    # Chaque ecran garde son propre pied : la consigne n'est pas la meme.
    assert confirmation.endswith(
        "\n\n<i>Les annonces correspondant à ces critères déclencheront une alerte Telegram.</i>"
    )
