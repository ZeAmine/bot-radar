"""Utilitaires purs de normalisation et de manipulation textuelle."""

import html as html_lib
import re
import unicodedata


def normalize_text(text: str | None) -> str:
    """Normalise une chaîne de caractères selon les règles strictes.

    Règles :
    - Décomposition Unicode (NFKD) et suppression des accents.
    - Conversion en minuscules.
    - Normalisation des apostrophes et guillemets.
    - Remplacement des tirets et tirets cadratins par des espaces.
    - Suppression de la ponctuation résiduelle.
    - Suppression des espaces multiples et rognage.
    """
    if not text:
        return ""

    nfkd = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = without_accents.lower()

    # Normalisation des apostrophes et tirets
    cleaned = re.sub(r"['’‘`ʼ]", " ", lowered)
    cleaned = re.sub(r"[-–—_]", " ", cleaned)

    # Remplacement de toute ponctuation non alphanumérique par un espace
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)

    # Réduction des espaces multiples
    tokens = cleaned.split()
    return " ".join(tokens)


def contains_keyword(text: str | None, keyword: str | None) -> bool:
    """Vérifie si un mot-clé ou expression apparaît en mot(s) entier(s) dans le texte."""
    if not text or not keyword:
        return False

    norm_kw = normalize_text(keyword)
    norm_text = normalize_text(text)
    if not norm_kw or not norm_text:
        return False

    words = norm_kw.split()
    pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
    return bool(re.search(pattern, norm_text))


def strip_html_tags(text: str) -> str:
    """Retire les balises d'un message Telegram et rend les entités HTML lisibles.

    Sert de repli quand Telegram refuse un message : l'utilisateur reçoit le texte
    brut plutôt que de rester bloqué sur un message d'attente.
    """
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text))
