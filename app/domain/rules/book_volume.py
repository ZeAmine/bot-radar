"""Identité d'un volume individuel demandé, indépendante du moteur IA."""

import re

from app.utils.text import normalize_text

_NUMBER = re.compile(r"\b(?:tomes?|t|livres?|volumes?|vols?)\.?\s*(\d{1,3})\b", re.IGNORECASE)
_COLLECTION = re.compile(
    r"\b(?:lots?|integrales?|coffrets?|omnibus|collection complete|collections completes)\b"
)


def volume_numbers(text: str) -> set[int]:
    numeral = r"(?:\d{1,3}|[ivxlcdm]+)\b"
    marker = r"\b(?:tomes?|t|livres?|volumes?|vols?)\.?\s*"
    continuation = re.compile(
        rf"(\s*(?:[-–—]|à|a|au|et|&|[,+])\s*|\s+)(?:{marker})?({numeral})",
        re.IGNORECASE,
    )
    numbers: set[int] = set()
    for match in re.finditer(rf"{marker}({numeral})", text, re.IGNORECASE):
        value = _volume_number(match.group(1))
        if value is None:
            continue
        numbers.add(value)
        position = match.end()
        while following := continuation.match(text, position):
            next_value = _volume_number(following.group(2))
            if re.match(
                r"\s*(?:€|eur\b|euros?\b|%|cm\b|mm\b|kg\b)",
                text[following.end():], re.IGNORECASE,
            ):
                break
            separator = following.group(1).strip().lower()
            if next_value is None or (not separator and not following.group(2).isdigit()):
                break
            if separator in {"-", "–", "—", "à", "a", "au"} and value < next_value:
                numbers.update(range(value, next_value + 1))
            else:
                numbers.add(next_value)
            value = next_value
            position = following.end()
    return numbers


def _volume_number(value: str) -> int | None:
    if value.isdigit():
        return int(value) or None
    value = value.upper()
    if not re.fullmatch(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", value):
        return None
    digits = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    return sum(
        -digits[c] if i + 1 < len(value) and digits[c] < digits[value[i + 1]] else digits[c]
        for i, c in enumerate(value)
    )


def is_volume_composition(text: str) -> bool:
    return len(volume_numbers(text)) > 1


def requested_volume(text: str) -> int | None:
    """Numéro explicitement demandé ; une plage n'est pas un volume individuel."""
    if is_volume_composition(text):
        return None
    numbers = volume_numbers(text)
    return next(iter(numbers)) or None if len(numbers) == 1 else None


def notice_volume(text: str) -> int | None:
    """Numéro porté par une notice, y compris un suffixe bibliographique « … 4 »."""
    if is_collection(text):
        return None
    number = requested_volume(text)
    if number is not None:
        return number
    if _NUMBER.search(text):
        return None
    match = re.search(r"\s(\d{1,3})\s*$", text)
    return int(match.group(1)) or None if match else None


def is_collection(text: str) -> bool:
    """Une collection ou un lot ne prouve pas l'identité d'un volume individuel."""
    return bool(_COLLECTION.search(normalize_text(text)))


def volume_title(title: str, number: int) -> str:
    """Titre d'affichage conservant explicitement le volume surveillé."""
    base = _NUMBER.sub("", title).strip(" ,.:;-–—")
    base = re.sub(rf"\s+{number}$", "", base).strip()
    return f"{base} - Volume {number}"
