"""Identité d'un volume individuel demandé, indépendante du moteur IA."""

import re

from app.utils.text import normalize_text

_NUMBER = re.compile(r"\b(?:tomes?|t|volumes?|vols?)\.?\s*(\d{1,3})\b", re.IGNORECASE)
_MULTIPLE = re.compile(r"\s*(?:[-–—]|à|a|et|&)\s*\d", re.IGNORECASE)
_COLLECTION = re.compile(
    r"\b(?:lots?|integrales?|coffrets?|omnibus|collection complete|collections completes)\b"
)


def requested_volume(text: str) -> int | None:
    """Numéro explicitement demandé ; une plage n'est pas un volume individuel."""
    matches = list(_NUMBER.finditer(text))
    if len(matches) != 1 or _MULTIPLE.match(text[matches[0].end() :]):
        return None
    number = int(matches[0].group(1))
    return number or None


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
