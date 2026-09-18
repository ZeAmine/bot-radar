"""Utilitaires d'extraction, de validation et de conversion pour les codes ISBN."""

import re


def isbn10_to_isbn13(isbn10: str) -> str | None:
    """Convertit un ISBN-10 en ISBN-13 standard (préfixe 978)."""
    cleaned = isbn10.replace("-", "").replace(" ", "").upper()
    if len(cleaned) != 10:
        return None

    core = "978" + cleaned[:9]
    checksum = 0
    for i, digit in enumerate(core):
        weight = 1 if i % 2 == 0 else 3
        checksum += int(digit) * weight
    check_digit = (10 - (checksum % 10)) % 10
    return core + str(check_digit)


def extract_clean_isbn(text: str) -> str | None:
    """Extrait et nettoie un ISBN-10 ou ISBN-13 depuis un texte ou une URL."""
    if not text:
        return None

    # 1. Chercher un ISBN-13 (ex: 9782803672080 ou 978-2-8036-7208-0)
    isbn13_match = re.search(
        r"(?:978|979)[-\s]?[0-9][-\s]?[0-9]{2,5}[-\s]?[0-9]{2,5}[-\s]?[0-9]", text
    )
    if isbn13_match:
        cleaned = re.sub(r"[^0-9]", "", isbn13_match.group(0))
        if len(cleaned) == 13:
            return cleaned

    # 2. Chercher un ISBN-10 avec frontières de mots
    isbn10_matches = re.findall(r"\b[0-9]{9}[0-9X]\b", text.upper().replace("-", " "))
    if isbn10_matches:
        cleaned = isbn10_matches[0]
        converted = isbn10_to_isbn13(cleaned)
        return converted or cleaned

    # 3. Repli si la chaîne sans ponctuation fait 13 ou 10 caractères
    cleaned_all = re.sub(r"[^0-9X]", "", text.strip().upper())
    if len(cleaned_all) == 13 and cleaned_all.isdigit():
        return cleaned_all
    if len(cleaned_all) == 10:
        converted = isbn10_to_isbn13(cleaned_all)
        return converted or cleaned_all

    return None
