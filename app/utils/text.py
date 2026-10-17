"""Utilitaires purs de normalisation et de manipulation textuelle."""

import html as html_lib
import re
import unicodedata
from functools import lru_cache


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

    cleaned = re.sub(r"['’‘`ʼ]", " ", lowered)
    cleaned = re.sub(r"[-–—_]", " ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    tokens = cleaned.split()
    return " ".join(tokens)


@lru_cache(maxsize=2048)
def _normalized_keyword(keyword: str) -> str:
    """Normalise un mot-clé une seule fois : le stock d'alertes le répète à l'identique."""
    return normalize_text(keyword)


@lru_cache(maxsize=2048)
def _keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    """Motif de frontière de mots pour un mot-clé, compilé une fois puis réutilisé.

    Le matching confronte chaque annonce à toutes les alertes actives, donc les mêmes
    mots-clés reviennent des centaines de fois par passe. Recompiler le motif à chaque
    confrontation était la moitié du temps CPU du chemin d'alerte.
    """
    norm_kw = _normalized_keyword(keyword)
    if not norm_kw:
        return None
    words = norm_kw.split()
    return re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")


def contains_keyword(text: str | None, keyword: str | None) -> bool:
    """Vérifie si un mot-clé ou expression apparaît en mot(s) entier(s) dans le texte.

    Le texte est normalisé ici : c'est le point d'entrée prudent, celui qu'on utilise
    hors du chemin d'alerte.
    """
    if not text or not keyword:
        return False
    norm_text = normalize_text(text)
    if not norm_text:
        return False
    return contains_normalized_keyword(norm_text, keyword)


def contains_normalized_keyword(normalized_text: str | None, keyword: str | None) -> bool:
    """Comme `contains_keyword`, mais pour un texte **déjà** normalisé.

    `normalize_text` est idempotent : normaliser deux fois donne le même résultat que
    normaliser une fois. Répéter la normalisation sur le texte de l'annonce — long, et
    identique des dizaines de fois par passe de matching — ne produisait donc aucun
    résultat différent, seulement du temps CPU perdu. Le texte de l'annonce étant
    construit une seule fois en amont (`searchable_text`), c'est ici qu'il doit être
    interrogé.
    """
    if not normalized_text or not keyword:
        return False
    pattern = _keyword_pattern(keyword)
    if pattern is None:
        return False
    return bool(pattern.search(normalized_text))


def strip_html_tags(text: str) -> str:
    """Retire les balises d'un message Telegram et rend les entités HTML lisibles.

    Sert de repli quand Telegram refuse un message : l'utilisateur reçoit le texte
    brut plutôt que de rester bloqué sur un message d'attente.
    """
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text))
