"""Utilitaires d'extraction, de validation et de conversion pour les codes ISBN."""

import re


def normalize_listing_isbn(raw: str | None) -> str | None:
    """Canonise une déclaration vendeur, sans en confirmer l'identité bibliographique."""
    if raw is None:
        return None
    code = re.sub(r"[-\s]", "", raw).upper()
    if not re.fullmatch(r"[0-9]{9}[0-9X]|(?:978|979)[0-9]{10}", code):
        return None
    return isbn_from_identifier(code)


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


# Préfixes GS1 qui désignent un livre. Un code-barres peut commencer par 2 (prix
# complémentaire) : c'est un autre produit, et l'attacher à une alerte ferait porter
# le mauvais identifiant au bon livre.
_BOOK_EAN_PREFIXES = ("978", "979")


def isbn_from_identifier(value: str | None) -> str | None:
    """L'ISBN-13 qu'un identifiant d'autorité porte, ou `None` s'il n'en porte pas.

    Une autorité écrit le même code de trois manières : `urn:isbn:9782812322495`,
    `ISBN : 978-2-8123-2249-5`, ou le code nu. Les deux premières portent le mot
    « ISBN » ; la troisième est un EAN-13, c'est-à-dire exactement le même code pour
    un livre — et c'est elle qui se faisait écarter, en silence, avec la notice
    entière. La comparaison par mots-clés laissait donc une source d'autorité
    partiellement muette, sans aucun signe pour l'expliquer.

    Un EAN de 13 chiffres est retenu seulement s'il valide sa clé de contrôle : sans
    elle, la seule garantie que ce soit un livre et non un code tronqué disparaît.
    """
    if not value:
        return None
    # Un ark de la BNF est une URL : les chiffres qu'il contient sont un identifiant
    # de notice, pas un livre. Le lire comme un ISBN lui prêterait un faux code.
    if "://" in value:
        return None
    cleaned = re.sub(r"[^0-9Xx]", "", value.upper())
    if len(cleaned) == 13:
        if not cleaned.startswith(_BOOK_EAN_PREFIXES):
            return None
        return cleaned if is_valid_isbn13(cleaned) else None
    if len(cleaned) == 10 and is_valid_isbn10(cleaned):
        return isbn10_to_isbn13(cleaned)
    return None


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
