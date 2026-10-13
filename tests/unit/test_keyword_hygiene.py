"""Le nettoyeur de mots-clés, testé sur les cas qui l'ont fait écrire.

Deux désastres réels servent de cas de référence : l'énumération du numéro d'un titre
(« … 2a » … « … 2z ») qui élargissait le matching à l'infini, et l'exclusion « 1 » nue
qui rejetait toute annonce chiffrée. Le module est pur : aucun mock n'est nécessaire.
"""

from __future__ import annotations

from app.domain.rules.keyword_hygiene import (
    MAX_EXCLUSIONS,
    MAX_VARIANTS,
    Reason,
    assess_exclusions,
    assess_variants,
    is_degenerate_exclusion,
    is_degenerate_variant,
    names_only_a_volume,
    sanitize_exclusions,
    sanitize_variants,
    variant_identity_verdict,
)

TITRE_NUMEROTE = "Absolute Martian Manhunter Vol. 2"


def test_variants_number_enumeration_is_dropped():
    """Le cas qui a produit « … 2a » … « … 2z » : la famille entière disparaît."""
    family = [f"absolute martian manhunter 2{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"]
    variants = sanitize_variants(
        [
            "absolute martian manhunter 2",
            "absolute martian manhunter tome 2",
            "absolute martian manhunter t2",
            *family,
        ],
        TITRE_NUMEROTE,
    )

    assert "absolute martian manhunter 2" in variants
    assert "absolute martian manhunter tome 2" in variants
    assert all(not is_degenerate_variant(v) for v in variants)
    assert len(variants) <= MAX_VARIANTS


def test_variants_with_full_title_and_letter_suffix_are_dropped():
    """« … vol 2z » répète le titre *et* énumère son numéro : les deux règles le coupent."""
    variants = sanitize_variants(
        ["absolute martian manhunter vol 2z", "absolute martian manhunter volume 2"],
        TITRE_NUMEROTE,
    )

    assert variants == ["absolute martian manhunter volume 2"]


def test_variant_extending_the_title_is_dropped():
    """« blasts » n'est pas une énumération, mais il répète « Blast » : il est écarté."""
    assert is_degenerate_variant("blasts") is False
    assert sanitize_variants(["blasts"], "Blast") == []


def test_variants_cap_and_normalised_dedup():
    """Au-delà du plafond, le moteur énumère ; deux écritures identiques n'en font qu'une.

    Les termes sont des fautes de frappe sur le titre (une lettre substituée à la fin) :
    ils passent la garde d'identité, ce qui est le point du plafond — il ne compte que
    des variantes exploitables. La fixture portait auparavant « black sad variante N »,
    dont le numéro n'appartient pas au titre : la garde d'identité les écarte désormais
    tous, et le plafond n'aurait plus rien à plafonner.
    """
    title = "Blacksad"
    many = [f"blacksa{letter}" for letter in "abcdefghijkl"]
    assert len(sanitize_variants(many, title)) == MAX_VARIANTS

    # « black sad » et « black  sad » se normalisent à l'identique : une seule survit.
    assert sanitize_variants(["black sad", "black  sad"], title) == ["black sad"]


def test_variants_still_drop_isbn_and_repeated_title():
    """Les règles historiques tiennent : un code d'ouvrage ou le titre lui-même est rejeté."""
    variants = sanitize_variants(
        [
            "les enfants perdus 9782214080627",
            "978-2-214-08062-7",
            "les enfants perdus 1989",
            "les enfants perdus la pensée universelle",
            "enfants perdus",
            "les enfnats perdus",
        ],
        "Les Enfants perdus",
    )

    assert variants == ["les enfnats perdus"]


def test_exclusions_numeric_and_single_char_are_dropped():
    """Une exclusion est un rejet : « 1 » rejetterait toute annonce chiffrée."""
    exclusions = sanitize_exclusions(["1", "r", "tome 1", "hs", "coffret vide"], "Blacksad")

    assert exclusions == ["tome 1", "hs", "coffret vide"]
    assert is_degenerate_exclusion("1") is True
    assert is_degenerate_exclusion("hs") is False


def test_exclusion_qualified_by_the_title_survives():
    """Une exclusion se qualifie par le titre : « deep it coffret » écarte les intégrales.

    Le titre ne se juge pas de la même façon selon la famille. Une variante qui le
    reprend n'apporte rien ; une exclusion qui le précise est la forme normale d'un
    rejet utile. Seule une exclusion *plus étroite* que le titre doit tomber, sinon
    elle rejette l'annonce cible elle-même.
    """
    title = "Deep It"
    exclusions = ["deep it coffret", "deep it intégrale", "coffret"]

    assert sanitize_exclusions(exclusions, title) == exclusions

    # Le même terme, du côté des variantes, répète le titre : il est écarté.
    assert sanitize_variants(["deep it coffret"], title) == []
    # Une exclusion qui tient dans le titre tairait l'annonce cible.
    assert sanitize_exclusions(["deep"], title) == []


def test_empty_title_decides_nothing():
    """Un titre vide ne doit rien décider, dans les deux familles.

    La comparaison est une inclusion de chaîne et `'' in terme` est toujours vrai :
    sans cette garde, un brouillon sans nom effacerait toutes ses variantes au
    premier passage du nettoyeur — c'est le cas que la recherche de mots-clés en
    arrière-plan peut atteindre, où le nom arrive par `draft.get("name") or ""`.
    """
    empty_titles: list[str | None] = ["", "   ", None]

    for title in empty_titles:
        assert sanitize_variants(["one piece", "tome 2"], title) == ["one piece", "tome 2"]
        assert sanitize_exclusions(["coffret", "figurine"], title) == ["coffret", "figurine"]


def test_exclusions_cap_is_preserved():
    """Le plafond d'exclusions ne bouge pas."""
    many = [f"exclusion {index}" for index in range(10)]
    assert len(sanitize_exclusions(many, "Blacksad")) == MAX_EXCLUSIONS


def test_degenerate_variant_does_not_flag_a_model_suffix():
    """« 13mini » n'est pas une énumération : la lettre n'est pas seule après le chiffre."""
    assert is_degenerate_variant("iphone 13mini") is False
    assert is_degenerate_variant("one piece 2a") is True


def test_sanitize_is_the_values_of_assess():
    """Les deux portes rendent le même verdict : l'une la liste, l'autre la motive.

    C'est le contrat qui permet de ne plus avoir à rejouer la règle pour l'expliquer :
    `sanitize_*` ne peut pas dire autre chose que `assess_*`.
    """
    values = ["absolute martian manhunter 2a", "tome 2", "9782864971412"]
    pairs = (
        (assess_variants, sanitize_variants),
        (assess_exclusions, sanitize_exclusions),
    )
    for assess, sanitize in pairs:
        outcome = assess(values, TITRE_NUMEROTE)
        assert outcome.values == sanitize(values, TITRE_NUMEROTE)
        # Les positions gardées préservent l'ordre d'entrée : sans quoi le
        # nettoyeur devrait les reconstruire, et le faire par valeur.
        assert list(outcome.kept_indexes) == sorted(outcome.kept_indexes)


def test_every_removed_term_carries_a_reason():
    """Aucun retrait sans motif : c'est l'invariant qui autorise une écriture.

    Un terme écarté sans raison laisserait l'utilisateur approuver une suppression sur
    une explication absente, ce qui est le pire des deux mondes.
    """
    outcome = assess_variants(
        ["absolute martian manhunter 2a", "", "9782864971412", "  ", "tome 2", "tome 2"],
        TITRE_NUMEROTE,
    )

    assert outcome.removals
    assert all(isinstance(removal.reason, Reason) for removal in outcome.removals)
    assert {r.raw for r in outcome.removals} >= {"", "  ", "9782864971412"}


def test_kept_indexes_and_removals_partition_the_input():
    """Chaque terme est gardé ou retiré : jamais les deux, jamais aucun des deux."""
    values = ["tome 2", "absolute martian manhunter 2a", "", "tome 2", "tome 3"]
    outcome = assess_variants(values, TITRE_NUMEROTE)

    removed_indexes = {r.index for r in outcome.removals}
    assert set(outcome.kept_indexes) | removed_indexes == set(range(len(values)))
    assert not set(outcome.kept_indexes) & removed_indexes


def test_the_reason_names_the_filter_that_decided():
    """Chaque motif est rattaché au filtre correspondant, pas seulement présent."""
    outcome = assess_variants(["absolute martian manhunter 2a", "", "x" * 50], TITRE_NUMEROTE)
    reasons = {r.reason for r in outcome.removals}

    assert Reason.ENUMERATION in reasons
    assert Reason.EMPTY in reasons


def test_a_volume_exclusion_carries_its_own_reason():
    """La règle du tome voisin motive elle aussi, sans qu'aucun appelant change.

    C'est l'invariant qui a coûté le moins cher, vérifié sur le cas qui l'a mis à
    l'épreuve : la règle d'ADR-030 est apparue **après** le nettoyeur, et
    `scripts/clean_alert_keywords.py` n'a reçu aucune ligne pour la connaître. Elle
    affiche pourtant « n'ajoute au titre qu'un numéro de tome » parce que le motif est
    écrit là où la décision se prend.
    """
    outcome = assess_exclusions(["absolute martian manhunter #2"], "Absolute Martian Manhunter")

    assert outcome.values == []
    assert [removal.reason for removal in outcome.removals] == [Reason.VOLUME_ONLY]
    # Le motif dépend du titre : `--only-degenerate` ne le franchit donc pas.
    assert Reason.VOLUME_ONLY.is_degenerate is False


def test_only_the_rule_decides_what_counts_as_degenerate():
    """`is_degenerate` est la frontière que `--only-degenerate` refuse de franchir.

    Les motifs issus du titre, d'un doublon ou du plafond ne sont pas dégénérés : ils
    dépendent du contexte, donc ils ne sont pas jouables sur une liste qu'on relit sans
    son contexte.
    """
    # Un titre sans rapport avec les termes : seul le doublon et le plafond peuvent
    # décider, sinon la règle du titre parlerait avant eux. Les termes sont donc des
    # fautes de frappe sur le titre lui-même — ils passent la garde d'identité, sans
    # quoi les motifs contextuels ne seraient jamais atteints.
    title = "Absolute Martian Manhunter"
    misspellings = [f"absolute martian manhunt{letter}" for letter in "abcdefghi"]
    values = [misspellings[0], misspellings[0], *misspellings[1:]]
    outcome = assess_variants(values, title)

    kinds = {r.reason for r in outcome.removals}
    assert Reason.DUPLICATE in kinds or Reason.LIMIT in kinds
    contextual = {Reason.DUPLICATE, Reason.LIMIT}
    assert not any(kind.is_degenerate for kind in kinds & contextual)

def test_exclusion_that_only_appends_a_volume_number_is_dropped():
    """Le cas réel : le titre plus un numéro n'identifie aucune confusion.

    Sur une alerte qui surveille la série, « … #2 » rejette l'annonce du tome 2 — celle
    qu'on veut voir. La famille entière tombe, et une exclusion qui ajoute autre chose
    que le numéro reste.
    """
    title = "Absolute Martian Manhunter"
    family = [f"absolute martian manhunter #{number}" for number in range(1, 7)]

    assert sanitize_exclusions(family, title) == []
    assert sanitize_exclusions(["absolute martian manhunter 2"], title) == []
    assert sanitize_exclusions(["absolute martian manhunter vol 2"], title) == []
    # Le qualificatif autre que le numéro est la forme utile du rejet : il survit.
    assert sanitize_exclusions(["absolute martian manhunter coffret"], title) == [
        "absolute martian manhunter coffret"
    ]


def test_a_year_is_not_a_volume_number():
    """« Blade Runner 2049 » est une autre œuvre, pas le tome 2049 du titre."""
    assert names_only_a_volume("blade runner 2049", "Blade Runner") is False
    assert sanitize_exclusions(["blade runner 2049"], "Blade Runner") == [
        "blade runner 2049"
    ]


def test_a_title_carrying_a_number_keeps_its_qualifying_exclusions():
    """Sur « One Piece tome 1 », exclure le tome 2 nomme un autre tome : c'est utile.

    La règle ne peut s'armer que sur un titre **sans** numéro : dès qu'il en porte un,
    le terme ne le commence plus, donc la garde est inerte.
    """
    assert names_only_a_volume("one piece tome 2", "One Piece tome 1") is False
    assert sanitize_exclusions(["one piece tome 2"], "One Piece tome 1") == [
        "one piece tome 2"
    ]


def test_an_exclusion_without_the_title_is_left_to_the_user():
    """« vol 2 » nu ne reprend pas le titre : le nettoyeur n'a pas à trancher à sa place.

    Le retirer supprimerait une exclusion volontaire — le reproche qu'ADR-026 adresse
    déjà au nettoyeur.
    """
    assert names_only_a_volume("vol 2", "Blacksad") is False
    assert sanitize_exclusions(["vol 2"], "Blacksad") == ["vol 2"]


def test_a_variant_naming_a_number_the_title_does_not_is_dropped():
    """Un nombre est une identité, pas une orthographe.

    « galaxy s24 » n'est pas une écriture de « Galaxy S23 », « blade runner 2047 » n'est
    pas « Blade Runner 2049 », et « bladerunner » en perd le millésime — l'alerte
    surveillerait alors l'œuvre de 1982. Comme une variante suffit à valider une annonce,
    chacune de ces écritures élargissait la veille à un autre produit.
    """
    assert sanitize_variants(["galaxy s24"], "Galaxy S23") == []
    assert sanitize_variants(["blade runner 2047"], "Blade Runner 2049") == []
    assert sanitize_variants(["bladerunner"], "Blade Runner 2049") == []
    assert sanitize_variants(["iphone 12"], "iPhone 13") == []


def test_a_glued_writing_keeps_the_titles_numbers():
    """Le nombre collé au mot est la même identité : c'est l'écriture à rattraper.

    La règle compare les suites de chiffres, jamais les tokens — sans quoi elle
    rejetterait exactement les graphies collées qu'une variante existe pour retrouver.
    """
    assert sanitize_variants(["iphone13"], "iPhone 13") == ["iphone13"]
    assert sanitize_variants(["pixel7pro"], "Pixel 7 Pro") == ["pixel7pro"]
    assert sanitize_variants(["bladerunner 2049"], "Blade Runner 2049") == [
        "bladerunner 2049"
    ]


def test_a_variant_too_far_from_the_title_is_dropped():
    """Un sigle n'est pas une orthographe : il ouvrirait la veille au hasard."""
    assert sanitize_variants(["op"], "One Piece") == []
    assert sanitize_variants(["amm"], "Absolute Martian Manhunter") == []
    assert sanitize_variants(["ip13"], "iPhone 13") == []


def test_a_typo_and_a_glued_word_survive_the_identity_guard():
    """Ce que la variante existe pour rattraper doit survivre : faute, collage, accent."""
    assert sanitize_variants(["blacksat"], "Blacksad") == ["blacksat"]
    assert sanitize_variants(["dne"], "Dune") == ["dne"]
    assert sanitize_variants(["one peice"], "One Piece") == ["one peice"]
    assert sanitize_variants(["onepiece"], "One Piece") == ["onepiece"]
    assert sanitize_variants(["couleurdeschoses"], "La couleur des choses") == [
        "couleurdeschoses"
    ]


def test_the_numbers_rule_neutralises_a_variant_that_lost_the_titles_number():
    """Une variante qui ne porte plus les nombres du titre ne valide rien, sans migration.

    C'est la raison d'être de la règle **en lecture** : elle s'applique aux alertes déjà
    enregistrées, sans colonne de provenance.

    Limite connue et mesurée : si ni l'ancienne ni la nouvelle œuvre ne portent de
    nombre, une variante obsolète n'est pas repérée. C'est le prix assumé de ne jamais
    refuser une variante qui porte les bons nombres — une occasion manquée coûte plus cher
    qu'une variante obsolète laissée en place.
    """
    assert sanitize_variants(["bladerunner"], "Blade Runner 2049") == []
    assert variant_identity_verdict("bladerunner", ("Blade Runner 2049",)) is (
        Reason.FOREIGN_NUMBER
    )
    # Personne ne porte de nombre : la règle ne tranche pas, et la variante reste.
    assert variant_identity_verdict("onepiece", ("Berserk", "berserk")) is None


def test_the_numbers_rule_is_the_one_the_matching_applies():
    """Le verdict d'identité ne juge que les nombres : c'est ce que le matching applique.

    Une faute de frappe, un mot collé, un alias de marque, une redondance : aucun de ces
    cas ne porte sur un nombre, donc aucun n'est refusé **au matching**. La règle est
    étroite par construction — refuser une variante déjà stockée coûte une annonce
    manquée, et c'est le plus cher des deux défauts.
    """
    assert variant_identity_verdict("blacksat", ("Blacksad",)) is None
    assert variant_identity_verdict("onepiece", ("One Piece",)) is None
    assert variant_identity_verdict("geforce rtx 4070", ("NVIDIA RTX 4070",)) is None
    assert variant_identity_verdict("op", ("One Piece",)) is None
    # Répéter le titre n'élargit rien : ce n'est pas non plus un refus du matching.
    assert variant_identity_verdict("blacksad", ("Blacksad",)) is None
    # Un nommage inédit se juge sur les mots, à l'écriture seulement.
    assert sanitize_variants(["blacksad"], "Blacksad") == []

    assert variant_identity_verdict("galaxy s24", ("Galaxy S23",)) is Reason.FOREIGN_NUMBER
    assert variant_identity_verdict("iphone13", ("iPhone 13",)) is None


def test_an_alias_of_the_same_product_keeps_the_numbers_and_the_shared_word():
    """« geforce rtx 4070 » est le même produit qu'un « NVIDIA RTX 4070 ».

    Le parcours d'ingestion de bout en bout tient ce cas pour acquis sur une alerte tech :
    la variante valide une annonce que le mot-clé « nvidia rtx 4070 dual » ne décrit pas.
    Une similarité globale — 0.5 contre 0.72 exigé — l'aurait rejeté, et l'alerte aurait
    cessé de matcher une annonce qu'elle attrape aujourd'hui. La reconnaissance par mot
    partagé est ce qui la laisse passer.
    """
    assert (
        variant_identity_verdict("geforce rtx 4070", ("NVIDIA RTX 4070",))
        is None
    )
    assert (
        sanitize_variants(
            ["geforce rtx 4070"],
            "NVIDIA RTX 4070",
            references=["nvidia rtx 4070 dual"],
        )
        == ["geforce rtx 4070"]
    )


def test_a_wordless_sigle_is_refused_at_write_time_only():
    """« op » et « amm » ne portent aucun mot du produit : le moteur ne les propose pas.

    Volontairement absents du verdict d'identité : le filtrage en lecture ne juge que les
    nombres, pour ne jamais refuser une variante déjà stockée.
    """
    assert sanitize_variants(["op"], "One Piece") == []
    assert sanitize_variants(["amm"], "Absolute Martian Manhunter") == []
    # Mais au matching, rien ne les arrête : c'est le prix assumé de la lecture prudente.
    assert variant_identity_verdict("op", ("One Piece",)) is None


def test_the_write_verdict_is_motivated_and_title_bound():
    """Chaque nouveau refus porte son motif, et ce motif dépend du titre ou des mots-clés.

    C'est ce qui interdit à `--only-degenerate` de les franchir : un rejeu aveugle ne peut
    pas emporter une variante sur une décision qui demandait le contexte.
    """
    sigle = assess_variants(["op"], "One Piece")
    assert [removal.reason for removal in sigle.removals] == [Reason.DRIFTED_WRITING]

    numbers = assess_variants(["galaxy s24"], "Galaxy S23")
    assert [removal.reason for removal in numbers.removals] == [Reason.FOREIGN_NUMBER]

    assert Reason.FOREIGN_NUMBER.is_degenerate is False
    assert Reason.DRIFTED_WRITING.is_degenerate is False


def test_a_variant_of_a_qualified_include_is_not_a_drift():
    """L'identité se juge sur l'intention entière ; la redondance, elle, reste au titre.

    « blacksad integral » reprend le titre « Blacksad » : le nettoyeur d'écriture l'écarte
    comme redondant, et c'est le comportement d'origine, inchangé. Le verdict d'identité,
    lui, ne le refuse pas : ni le titre seul ni l'inclusion qualifiée ne portent de nombre
    qu'il changerait.
    """
    assert variant_identity_verdict("blacksad integral", ("Blacksad",)) is None
    assert (
        variant_identity_verdict("blacksad integral", ("Blacksad", "blacksad intégrale"))
        is None
    )
    # La redondance, elle, se juge bien sur les références : reprendre une inclusion
    # n'ajoute rien, et l'écriture le refuse.
    assert sanitize_variants(
        ["blacksad integral"], "Blacksad", references=["blacksad intégrale"]
    ) == []


def test_a_variant_repeating_an_include_adds_nothing():
    """Une variante qui reprend l'inclusion n'élargit rien : l'inclusion matche déjà."""
    assert sanitize_variants(
        ["blacksad"], "Blacksad", references=["blacksad intégrale"]
    ) == []


def test_the_new_word_rule_is_the_one_the_write_path_applies():
    """Le garde-fou `_shares_no_distinctive_word` vit à l'écriture, pas au matching.

    C'est la seule des nouvelles règles qui s'arme sans le contexte du titre : elle doit
    donc se taire quand le titre est vide, exactement comme `_swallows_the_title`.
    """
    for title in ("", "   ", None):
        assert sanitize_variants(["galaxy s24", "op"], title) == ["galaxy s24", "op"]
        assert variant_identity_verdict("galaxy s24", (title,)) is None
