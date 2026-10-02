"""Utilitaires d'extraction, de validation et de conversion pour les codes ISBN."""

import re


def normalize_isbn(raw: str | None) -> str | None:
    """Nettoie une saisie brute en ISBN-10 ou ISBN-13 (tirets, espaces, points, casse)."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9Xx]", "", raw.upper())
    if len(cleaned) in (10, 13):
        return cleaned
    return None


def is_valid_isbn10(isbn: str | None) -> bool:
    """Vérifie la clé de contrôle modulo 11 d'un ISBN-10."""
    if not isbn or len(isbn) != 10:
        return False
    total = 0
    for position, char in enumerate(isbn):
        value = 10 if char == "X" else int(char) if char.isdigit() else -1
        if value < 0:
            return False
        total += value * (10 - position)
    return total % 11 == 0


def is_valid_isbn13(isbn: str | None) -> bool:
    """Vérifie la clé de contrôle (somme pondérée 1/3) d'un ISBN-13."""
    if not isbn or len(isbn) != 13 or not isbn.isdigit():
        return False
    total = 0
    for position, char in enumerate(isbn[:12]):
        weight = 1 if position % 2 == 0 else 3
        total += int(char) * weight
    return (10 - (total % 10)) % 10 == int(isbn[12])


def is_valid_isbn(isbn: str | None) -> bool:
    """Indique si le code est un ISBN-10 ou ISBN-13 valide au sens de sa clé de contrôle."""
    return is_valid_isbn10(isbn) or is_valid_isbn13(isbn)


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


def _isbn_candidates(text: str) -> list[str]:
    """Liste les codes ISBN plausibles présents dans un texte, du plus explicite au plus large."""
    candidates: list[str] = []

    # 1. ISBN-13 explicites (ex: 9782803672080 ou 978-2-8036-7208-0)
    isbn13_pattern = r"(?:978|979)[-\s]?[0-9][-\s]?[0-9]{2,5}[-\s]?[0-9]{2,5}[-\s]?[0-9]"
    for match in re.finditer(isbn13_pattern, text):
        cleaned = re.sub(r"[^0-9]", "", match.group(0))
        if len(cleaned) == 13:
            candidates.append(cleaned)

    # 2. ISBN-10 avec frontières de mots
    for raw10 in re.findall(r"\b[0-9]{9}[0-9X]\b", text.upper().replace("-", " ")):
        # La conversion recalcule la clé de contrôle : ne convertir que si l'ISBN-10 est valide.
        converted = isbn10_to_isbn13(raw10) if is_valid_isbn10(raw10) else None
        candidates.append(converted or raw10)

    # 3. Repli si la chaîne entière est un code compact
    compact = re.sub(r"[^0-9X]", "", text.strip().upper())
    if compact.isdigit() and len(compact) == 13:
        candidates.append(compact)
    elif len(compact) == 10:
        candidates.append((isbn10_to_isbn13(compact) or compact) if is_valid_isbn10(compact)
                          else compact)

    return candidates


def extract_clean_isbn(text: str) -> str | None:
    """Extrait un ISBN-10 ou ISBN-13 *valide* (clé de contrôle vérifiée) depuis un texte.

    Un code mal saisi (faute de frappe sur un chiffre) est rejeté plutôt que propagé :
    l'ISBN devient une contrainte de matching stricte, il ne doit jamais être deviné.
    """
    if not text:
        return None
    for candidate in _isbn_candidates(text):
        if is_valid_isbn(candidate):
            return candidate
    return None