"""Décision de cote : quatre états nommés, un prix bas daté, jamais « Non renseigné ».

L'aperçu de création affichait « Cote indisponible / Dernière offre connue : Non
renseigné » alors que la collecte avait des offres. Ces tests fixent la décision
qui manquait : une offre retenue, ou le fait exact qui explique son absence.
"""

from __future__ import annotations

import pytest

from app.bot.labels import cote_line, low_line
from app.domain.rules.cote_outcome import (
    ETATS_COTE_ACCEPTES,
    DerniereOffre,
    OffreCote,
    ReleveHistorique,
    select_cote_offer,
    select_historical_low,
)


def offre(marchand: str, total: int, etat: str | None = "TBE") -> OffreCote:
    return OffreCote(marchand=marchand, etat=etat, total_cents=total)


def test_the_cheapest_accepted_offer_is_retained_with_shipping_included():
    outcome = select_cote_offer([offre("Momox", 2369), offre("RecycLivre", 2990)])

    assert outcome.state == "cote_trouvee"
    assert outcome.offre == offre("Momox", 2369)


def test_the_price_cap_filters_and_never_rewrites_a_price():
    """Le plafond de l'alerte ne baisse pas une cote : il l'écarte, et le dit."""
    outcome = select_cote_offer([offre("Momox", 2369)], prix_max=2000)

    assert outcome.state == "offres_ecartees_par_les_filtres"
    assert outcome.moins_chere_ecartee_cents == 2369
    assert outcome.offre is None


def test_a_brand_new_copy_is_accepted_for_a_recent_volume():
    """Écarter le neuf refuserait la seule offre d'un tome récent."""
    assert select_cote_offer([offre("Momox", 1880, "NAE")]).state == "cote_trouvee"
    assert select_cote_offer([offre("Momox", 1880, "NSE")]).state == "cote_trouvee"


def test_a_worn_copy_is_kept_out_of_the_cote_but_named_with_its_price():
    outcome = select_cote_offer([offre("Bouquin", 900, "BE"), offre("Autre", 1200, "Mauvais")])

    assert outcome.state == "offres_ecartees_par_les_filtres"
    assert outcome.offres_examinees == 2
    assert outcome.moins_chere_ecartee_cents == 900


def test_an_unstated_condition_is_not_a_refused_condition():
    """Une cote en base garde le prix et le marchand, pas le libellé d'état."""
    outcome = select_cote_offer([offre("Momox", 2059, etat=None)])

    assert outcome.state == "cote_trouvee"
    assert "Non renseigné" not in cote_line(outcome)


def test_an_empty_collection_is_a_named_absence_not_a_failure():
    assert select_cote_offer([]).state == "aucune_offre"


def test_an_unanswered_source_is_a_failure_not_an_absence():
    """`None` = la source n'a pas répondu ; `[]` = elle a répondu « rien »."""
    assert select_cote_offer(None).state == "erreur_recuperation"


def test_a_previous_price_accompanies_the_absence_without_becoming_the_cote():
    outcome = select_cote_offer(None, derniere_offre=DerniereOffre(cents=2059))

    assert outcome.state == "erreur_recuperation"
    line = cote_line(outcome)
    assert "récupération impossible" in line
    assert "dernière offre connue : 20,59 €" in line


def test_a_found_cote_never_quotes_a_previous_price():
    outcome = select_cote_offer([offre("Momox", 2369)], derniere_offre=DerniereOffre(cents=2059))

    assert "dernière offre connue" not in cote_line(outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (select_cote_offer([offre("Momox Shop", 2369)]),
         "<b>Cote :</b> 23,69 € (Momox Shop)"),
        (select_cote_offer([]), "<b>Cote :</b> aucune offre trouvée"),
        (select_cote_offer(None),
         "<b>Cote :</b> récupération impossible, la source n'a pas répondu"),
        (select_cote_offer([offre("Bouquin", 900, "BE")], prix_max=5000),
         "<b>Cote :</b> offres écartées par les filtres (1 offres, la moins chère à 9,00 €)"),
    ],
)
def test_each_state_has_its_own_sentence(outcome, expected):
    """Quatre états, quatre phrases : aucune ne dit « Non renseigné »."""
    assert cote_line(outcome) == expected


def test_the_four_states_are_all_reachable_and_distinct():
    states = {
        select_cote_offer([offre("A", 1000)]).state,
        select_cote_offer([]).state,
        select_cote_offer(None).state,
        select_cote_offer([offre("A", 900, "BE")]).state,
    }

    assert len(states) == 4


def test_the_historical_low_is_the_cheapest_observation():
    """Le minimum sur douze mois reste la décision : la forme courte ne choisit pas."""
    outcome = select_historical_low([
        ReleveHistorique(etat="Très bon état +", total_cents=1999),
        ReleveHistorique(etat="Comme neuf +", total_cents=2450),
    ])

    assert outcome.state == "prix_bas_trouve"
    assert outcome.cents == 1999
    assert outcome.etat == "Très bon état +"


def test_a_found_low_is_its_amount_alone_under_the_shared_label():
    """« Prix bas : 19,99 € » : ni état marchand, ni port, ni fenêtre entre parenthèses.

    La simplification ne retire que la mise en forme : le palier retenu reste
    celui de la décision, et c'est lui que la ligne ne nomme plus.
    """
    outcome = select_historical_low([ReleveHistorique(etat="Très bon état +", total_cents=1999)])

    line = low_line(outcome)

    assert line == "<b>Prix bas :</b> 19,99 €"
    assert outcome.etat == "Très bon état +"
    for wording in ("port inclus", "très bon état", "1 an", "constaté"):
        assert wording not in line


def test_a_found_cote_names_the_amount_then_the_merchant_in_parentheses():
    """« Cote : 12,34 € (Recyclivre) », et rien d'autre sur la ligne."""
    line = cote_line(select_cote_offer([offre("Recyclivre", 1234, "TBE")]))

    assert line == "<b>Cote :</b> 12,34 € (Recyclivre)"
    for wording in ("port inclus", "très bon état"):
        assert wording not in line


def test_a_found_cote_escapes_the_merchant_it_puts_in_parentheses():
    """Un nom de marchand reste une donnée : il n'écrit pas le formatage."""
    line = cote_line(select_cote_offer([offre("<Bazar & Cie>", 900, None)]))

    assert line == "<b>Cote :</b> 9,00 € (&lt;Bazar &amp; Cie&gt;)"


def test_every_line_without_a_price_keeps_its_wording():
    """Cas sans prix : mot pour mot, la forme courte ne les touche pas."""
    assert cote_line(select_cote_offer([])) == "<b>Cote :</b> aucune offre trouvée"
    assert cote_line(select_cote_offer(None)) == (
        "<b>Cote :</b> récupération impossible, la source n'a pas répondu"
    )
    assert cote_line(select_cote_offer(None, derniere_offre=DerniereOffre(cents=2059))) == (
        "<b>Cote :</b> récupération impossible, la source n'a pas répondu"
        " (dernière offre connue : 20,59 €)"
    )
    assert cote_line(select_cote_offer([offre("Bouquin", 900, "BE")], prix_max=5000)) == (
        "<b>Cote :</b> offres écartées par les filtres (1 offres, la moins chère à 9,00 €)"
    )
    assert low_line(select_historical_low([])) == (
        "<b>Prix bas constaté (1 an) :</b> aucun relevé sur 12 mois"
    )
    assert low_line(select_historical_low(None)) == (
        "<b>Prix bas constaté (1 an) :</b> historique indisponible"
    )


def test_a_tie_on_price_cites_the_very_good_tier_not_the_alphabetical_first():
    """Deux paliers au même prix : c'est le palier de la cote qui est cité.

    La capture du 27/10/2026 publie « Comme neuf + » et « Très bon état + » au même
    prix de 19,99 €. Trier par libellé rendait « comme neuf + » par accident de
    l'alphabet ; à prix égal, le palier que la cote du jour nomme l'emporte.
    """
    outcome = select_historical_low([
        ReleveHistorique(etat="Comme neuf +", total_cents=1999),
        ReleveHistorique(etat="Très bon état +", total_cents=1999),
    ])

    assert outcome.etat == "Très bon état +"
    assert low_line(outcome) == "<b>Prix bas :</b> 19,99 €"


def test_a_state_outside_the_accepted_history_words_is_not_a_low():
    outcome = select_historical_low([ReleveHistorique(etat="Bon état", total_cents=999)])

    assert outcome.state == "aucun_releve_12_mois"


def test_twelve_months_without_data_is_a_fact():
    assert select_historical_low([]).state == "aucun_releve_12_mois"
    assert (
        low_line(select_historical_low([]))
        == "<b>Prix bas constaté (1 an) :</b> aucun relevé sur 12 mois"
    )


def test_a_failed_history_read_is_not_an_empty_history():
    """Une réponse vide ou invalide n'est pas « aucun relevé en douze mois »."""
    outcome = select_historical_low(None)

    assert outcome.state == "erreur_recuperation_historique"
    assert low_line(outcome) == "<b>Prix bas constaté (1 an) :</b> historique indisponible"


def test_the_three_history_states_are_distinct():
    states = {
        select_historical_low([ReleveHistorique(etat="Très bon état +", total_cents=1999)]).state,
        select_historical_low([]).state,
        select_historical_low(None).state,
    }

    assert len(states) == 3


def test_the_accepted_states_are_the_three_of_the_reference_frame():
    assert frozenset({"NAE", "NSE", "TBE"}) == ETATS_COTE_ACCEPTES
