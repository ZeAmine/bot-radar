"""Tests unitaires pour le formatage des messages d'alertes."""

import uuid

from app.bot.formatter import format_alert_card, format_cents_to_euros
from app.bot.handlers.alerts_helpers import RECAP_TITLE, creation_card
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.utils.text import strip_html_tags


def test_format_cents_to_euros():
    assert format_cents_to_euros(1500) == "15,00 €"
    assert format_cents_to_euros(999) == "9,99 €"
    assert format_cents_to_euros(0) == "0,00 €"
    assert format_cents_to_euros(None) == "Non renseigné"


def test_format_alert_card():
    category = Category(
        id=uuid.uuid4(),
        slug="bd_comics",
        label="BD & Comics",
        emoji="📚",
        is_active=True,
    )
    alert = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        category_id=category.id,
        name="Blacksad intégrale",
        include_keywords=["blacksad", "intégrale"],
        exclude_keywords=["tome 1"],
        variants=[],
        max_item_price_cents=1500,
        accepted_conditions=["Très bon état"],
        status=AlertStatus.ACTIVE,
        category=category,
        isbn="9782205089752",
        vinted_search_url="https://www.vinted.fr/catalog?search_text=blacksad",
    )

    card = format_alert_card(alert, current_index=0, total_count=1)
    assert "📚 <b>Blacksad intégrale</b>" in card
    assert "<b>ISBN :</b> <code>9782205089752</code>" in card
    assert "<b>Prix maximum :</b> 15,00 €" in card
    assert "<b>Mots-clés inclus :</b> blacksad, intégrale" in card
    assert "<b>Mots-clés exclus :</b> tome 1" in card
    assert "<b>Statut :</b> Active" in card
    assert "<b>États acceptés :</b> TBE" in card
    assert (
        '<b>Lien :</b> <a href="https://www.vinted.fr/catalog?search_text=blacksad">'
        "Consulter la recherche</a>"
        in card
    )


def test_format_alerts_list_text():
    from app.bot.handlers.alerts import format_alerts_list_text

    cat_books = Category(id=uuid.uuid4(), slug="bd_comics",
                         label="Livres", emoji="📚", is_active=True)
    cat_tech = Category(id=uuid.uuid4(), slug="tech",
                        label="Électronique", emoji="💻", is_active=True)

    alert_book = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Astérix - Tome 40",
        isbn="9782014001150",
        max_item_price_cents=800,
        status=AlertStatus.ACTIVE,
        category=cat_books,
    )
    alert_tech = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Switch Mario Kart 8",
        isbn=None,
        max_item_price_cents=3000,
        status=AlertStatus.ACTIVE,
        category=cat_tech,
    )

    text = format_alerts_list_text(
        [alert_book, alert_tech],
        current_page=0,
        total_count=2,
        category_label="📚 Livres",
    )

    assert "Mes alertes — 📚 Livres" in text
    assert "Page 1/1 — 2 alertes" in text
    # Chaque entrée : numéro, titre, statut, prix maximum — l'ISBN reste dans la fiche
    assert "1. 📚 <b>Astérix - Tome 40</b> : 8€ (Active)" in text
    assert "2. 💻 <b>Switch Mario Kart 8</b> : 30€ (Active)" in text
    assert "9782014001150" not in text


def _recap(**overrides):
    """Récap minimal, avec des valeurs neutres sauf celles fournies."""
    draft = {
        "name": "La couleur des choses",
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "isbn": None,
        "category_slug": "bd_comics",
        "max_item_price_cents": 1200,
        "include_keywords": ["La couleur des choses"],
        "exclude_keywords": ["figurine"],
        "selected_conditions": None,
        "variants": None,
        "is_series": False,
        "isbn_note": None,
        "edition_alternatives": None,
    }
    draft.update(overrides)
    return creation_card(draft)


def test_recap_escapes_model_output():
    """Un mot-clé balisé par le modèle ne doit jamais casser le HTML du récap.

    Telegram répond sinon « can't parse entities » et rejoue l'update : l'utilisateur
    reste bloqué sur « Recherche IA en cours ».
    """
    recap = _recap(
        include_keywords=["<b>Tomes</b>", "PS5 & Switch"],
        exclude_keywords=["<i>figurine</i>"],
        variants=["<code>couleurdeschoses</code>"],
    )

    assert "<b>Tomes</b>" not in recap
    assert "<i>figurine</i>" not in recap
    assert "<code>couleurdeschoses</code>" not in recap
    assert "&lt;b&gt;Tomes&lt;/b&gt;" in recap
    assert "PS5 &amp; Switch" in recap
    assert "&lt;i&gt;figurine&lt;/i&gt;" in recap
    assert "&lt;code&gt;couleurdeschoses&lt;/code&gt;" in recap
    # Les balises du récap lui-même restent intactes.
    assert "<b>Mots-clés inclus :</b>" in recap
    assert "<b>Mots-clés exclus :</b>" in recap


def test_recap_escapes_model_conditions():
    """Les états acceptés viennent aussi du modèle : ils sont échappés."""
    recap = _recap(selected_conditions=["Neuf & scellé", "Très bon <état>"])

    assert "Neuf &amp; scellé" in recap
    assert "Très bon &lt;état&gt;" in recap
    assert "<état>" not in recap


def test_recap_escapes_isbn_note_alongside_keywords():
    """Le chemin sans ISBN vérifié signale la provenance : rien ne doit être nu."""
    recap = _recap(include_keywords=["<b>ASTON</b>"], isbn_note="non confirmé <b>BNF</b>")

    assert "<b>ASTON</b>" not in recap
    assert "non confirmé &lt;b&gt;BNF&lt;/b&gt;" in recap


def test_recap_opens_on_a_single_fixed_title():
    """La carte s'ouvre sur un titre, le même partout.

    Le message d'attente « 🪄 Création d'alerte en langage naturel » cède la place à
    la carte : sans titre, le récap commence froidement sur l'ISBN. Mais le titre qui
    le remplace est fixe. Les en-têtes de parcours avaient été supprimés parce qu'ils
    multipliaient les rendus d'un seul brouillon ; un titre qui dépendrait des données
    ou du chemin redeviendrait exactement ça.
    """
    recap = _recap()

    assert recap.startswith(f"{RECAP_TITLE}\n\n")
    assert recap.count(RECAP_TITLE) == 1

    # Ni le titre ni le contenu ne dépendent du jeu de données fourni.
    other = _recap(
        isbn="9782369903086",
        is_series=True,
        edition_alternatives=[
            {"isbn13": "9782369903086", "publisher": "Çà et là", "publication_year": 2022},
            {"isbn13": "9782369904151", "publisher": "Futuropolis", "publication_year": 2018},
        ],
    )

    assert other.splitlines()[0] == recap.splitlines()[0] == RECAP_TITLE


def test_recap_always_closes_on_the_confirmation_it_expects():
    """Le pied de confirmation appartient à la carte, pas à un point d'appel.

    Tous les chemins qui affichent ce récap finissent sur le même clavier. Quand le
    pied vivait chez l'appelant, `/add` en langage naturel l'affichait et le récap
    manuel l'avait perdu : la question à poser se trouvait deux fois, et seulement
    à un endroit. Ici elle est écrite une fois, donc présente partout.
    """
    recap = _recap()

    assert recap.rstrip().endswith(
        "<i>Valide les critères ci-dessus avant de créer l'alerte.</i>"
    )
    assert recap.count("Valide les critères ci-dessus avant de créer l'alerte.") == 1


def test_strip_html_tags_produces_readable_plain_text():
    """Le repli anti-boucle rend un texte lisible, entités décodées."""
    plain = strip_html_tags("🎯 <b>Nom :</b> PS5 &amp; Switch<br>prix 12 €")

    assert plain == "🎯 Nom : PS5 & Switchprix 12 €"
    assert "<" not in plain
    assert "&amp;" not in plain


def test_recap_says_an_unfound_isbn_instead_of_saying_nothing():
    """L'absence d'ISBN est une information : elle s'affiche.

    La ligne disparaissait, et le récap laissait croire à un oubli plutôt qu'à un
    ouvrage qu'aucune autorité ne catalogue — le cas même de la capture remontée.
    Elle ne nomme plus la BNF : depuis la troisième source (ADR-033), elle n'est plus
    la seule à avoir été interrogée, et l'avertissement de tête porte le *pourquoi*.
    """
    recap = _recap(isbn=None, isbn_note=None)

    assert "<b>ISBN :</b> introuvable" in recap


def test_recap_keeps_the_series_indication_out_of_the_information_list():
    """Une série n'a pas d'ISBN : l'indication le dit, dans son propre bloc.

    L'ancien récap portait `non utilisé — série multi-tomes` dans la liste des
    informations — la même phrase que l'indication, une deuxième fois, à un endroit qui
    la faisait lire comme une donnée de l'ouvrage. La phrase n'apparaît plus qu'une
    fois, en italique, juste sous le titre de la carte.
    """
    recap = _recap(isbn=None, is_series=True)
    lines = recap.splitlines()

    assert lines[0] == RECAP_TITLE
    assert lines[1] == ""
    assert lines[2] == "<i>Série multi-tomes (veille sur tous les tomes)</i>"
    assert lines[3] == ""
    assert lines[4].startswith("<b>Titre :</b>")
    assert recap.count("Série multi-tomes") == 1
    assert "<b>ISBN :</b>" not in recap
    assert "introuvable" not in recap


def test_recap_says_nothing_about_isbn_where_none_can_exist():
    """Un objet n'a pas d'ISBN à chercher : la ligne n'aurait pas de sujet."""
    recap = _recap(isbn=None, category_slug="tech")

    assert "<b>ISBN :</b>" not in recap


def test_list_preserves_numbering_and_escapes_paused_titles():
    from app.bot.handlers.alerts import format_alerts_list_text

    alert = Alert(name="Livre <rare>", status=AlertStatus.PAUSED,
                  max_item_price_cents=1250,
                  category=Category(slug="bd_comics", emoji="📚"))
    text = format_alerts_list_text([alert], current_page=2, total_count=141)
    assert "Page 3/15 — 141 alertes" in text
    assert "21. 📚 <b>Livre &lt;rare&gt;</b> : 12,50€ (En pause)" in text
