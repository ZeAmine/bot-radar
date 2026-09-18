"""Tests unitaires pour le formatage des messages d'alertes."""

import uuid

from app.bot.formatter import format_alert_card, format_alert_recap, format_cents_to_euros
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category


def test_format_cents_to_euros():
    assert format_cents_to_euros(1500) == "15,00 €"
    assert format_cents_to_euros(999) == "9,99 €"
    assert format_cents_to_euros(0) == "0,00 €"
    assert format_cents_to_euros(None) == "non défini"


def test_format_alert_recap():
    recap = format_alert_recap(
        emoji="📚",
        name="Blacksad intégrale",
        include_keywords=["blacksad", "intégrale"],
        exclude_keywords=["tome 1", "poche"],
        variants=["blacksad integral"],
        max_price_cents=1500,
        accepted_conditions=["Très bon état", "Neuf sans étiquette"],
    )

    assert "📚 Nouvelle alerte" in recap
    assert "Nom : Blacksad intégrale" in recap
    assert "Mots-clés : blacksad, intégrale" in recap
    assert "Exclusions : tome 1, poche" in recap
    assert "Variantes : blacksad integral" in recap
    assert "Prix maximum article : 15,00 €" in recap
    assert "États : Très bon état, Neuf sans étiquette" in recap


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
    assert "ISBN : 9782205089752" in card
    assert "Prix max : 15,00 €" in card
    assert "Inclus : blacksad, intégrale" in card
    assert "Exclus : tome 1" in card
    assert "Statut : 🟢 Active" in card
    assert 'Lien : <a href="https://www.vinted.fr/catalog?search_text=blacksad">Consulter la recherche</a>' in card


def test_format_alerts_list_text():
    from app.bot.handlers.alerts import format_alerts_list_text

    cat_books = Category(id=uuid.uuid4(), slug="bd_comics", label="Livres", emoji="📚", is_active=True)
    cat_tech = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻", is_active=True)

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

    assert "Vos alertes surveillées — 📚 Livres" in text
    assert "Page 1/1 — 2 alertes" in text
    # Alerte avec ISBN
    assert "<b>1.</b> 🟢 📚 <b>Astérix - Tome 40</b>" in text
    assert "↳ ISBN : <code>9782014001150</code> • Max : 8,00 €" in text
    # Alerte sans ISBN (ne doit PAS afficher 'sans ISBN')
    assert "<b>2.</b> 🟢 💻 <b>Switch Mario Kart 8</b>" in text
    assert "↳ Max : 30,00 €" in text
    assert "sans ISBN" not in text
