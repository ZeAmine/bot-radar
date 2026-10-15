"""La numérotation des décisions reste unique et continue après une fusion."""

import re
from pathlib import Path

import pytest


def validate_sequence(document: str) -> None:
    numbers = [int(n) for n in re.findall(r"^#{1,6}\s+ADR-(\d+)\s*:", document, re.M)]
    assert numbers, "Aucune déclaration ADR"
    duplicates = sorted(n for n in set(numbers) if numbers.count(n) > 1)
    missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
    assert not duplicates, f"ADR dupliqués : {duplicates}"
    assert not missing, f"ADR manquants : {missing}"


def test_project_adrs_are_unique_and_continuous():
    directory = Path(__file__).resolve().parents[2] / "docs" / "decisions"
    documents = [
        path.read_text()
        for path in sorted(directory.glob("*.md"))
        if path.name != "README.md"
    ]
    assert documents, "Aucun recueil ADR"
    assert all(re.search(r"^#{1,6}\s+ADR-\d+\s*:", doc, re.M) for doc in documents), (
        "Recueil sans déclaration ADR"
    )
    validate_sequence("\n".join(documents))


@pytest.mark.parametrize(
    "document",
    [
        "### ADR-001 : Un\n### ADR-001 : Deux",
        "### ADR-001 : Un\n### ADR-003 : Trois",
        "### ADR-002 : Deux",
        "Texte sans décision",
    ],
)
def test_invalid_sequences_fail(document):
    with pytest.raises(AssertionError):
        validate_sequence(document)


def test_references_do_not_count_as_declarations():
    validate_sequence("Voir ADR-001, ADR-003\n### ADR-001 : Un\n### ADR-002 : Deux")
