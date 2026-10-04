"""Module d'analyse heuristique et d'extraction de tomes et lots pour annonces de BD."""

import re
import unicodedata
from dataclasses import dataclass, field


def _normalize(text: str) -> str:
    """Normalise une chaîne : minuscules, sans accents, sans altérer ponctuation et tirets."""
    norm = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in norm if not unicodedata.combining(c))
    return stripped.lower().strip()


# Dictionnaire de correspondance sous-titre -> numéro de tome pour les séries majeures
SERIES_SUBTITLES: dict[str, dict[str, int]] = {
    "rip": {
        "derrick": 1,
        "je ne survivrai pas a la mort": 1,
        "maurice": 2,
        "les mouches ne mentent pas": 2,
        "mouches ne mentent pas": 2,
        "ahmed": 3,
        "pourrissant dans la neige": 3,
        "albert": 4,
        "priere pour les vivants": 4,
        "fanette": 5,
        "mauvaise graine": 5,
        "eugene": 6,
        "toutes les bonnes choses ont une fin": 6,
    },
    "blacksad": {
        "quelque part entre les ombres": 1,
        "arctic-nation": 2,
        "arctic nation": 2,
        "ame rouge": 3,
        "enfer le silence": 4,
        "l enfer le silence": 4,
        "amarillo": 5,
        "alors tout tombe seconde partie": 7,
        "alors tout tombe deuxieme partie": 7,
        "alors tout tombe 2": 7,
        "alors tout tombe premiere partie": 6,
        "alors tout tombe 1": 6,
        "alors tout tombe": 6,
    },
    "le chateau des animaux": {
        "miss bengalore": 1,
        "les marguerites d hiver": 2,
        "la nuit des justes": 3,
        "le sang du roi": 4,
    },
    "blast": {
        "grasse carcasse": 1,
        "apocalypse selon saint jacky": 2,
        "saint jacky": 2,
        "la tete la premiere": 3,
        "bouddhistes se trompent": 4,
    },
    "l odyssee d hakim": {
        "de la syrie a la turquie": 1,
        "syrie": 1,
        "de la turquie a la grece": 2,
        "grece": 2,
        "de la macedoine a la france": 3,
        "france": 3,
    },
    "tyler cross": {
        "black rock": 1,
        "angola": 2,
        "miami": 3,
    },
    "1629": {
        "apothicaire du diable": 1,
        "martyrs de la baie": 2,
    },
    "dans la tete de sherlock holmes": {
        "ticket scandaleux": 1,
    },
    "le combat ordinaire": {
        "quantites negligeables": 2,
        "les quantites negligeables": 2,
        "ce qui est precieux": 3,
        "planter des clous": 4,
    },
    "le rapport de brodeck": {
        "l autre": 1,
        "autre": 1,
        "l indicible": 2,
        "indicible": 2,
    },
    "slava": {
        "apres la chute": 1,
        "les nouveaux russes": 2,
        "nouveaux russes": 2,
        "un enfer pour un autre": 3,
        "enfer pour un autre": 3,
    },
    "islander": {
        "l exil": 1,
        "exil": 1,
        "l alliance": 2,
        "alliance": 2,
    },
    "les gorilles du general": {
        "septembre 59": 1,
    },
    "dead train": {
        "la prophetie de la vierge noire": 1,
        "vierge noire": 1,
    },
}

# Chiffres romains courants pour tomes (1 à 20)
ROMAN_NUMERALS: dict[str, int] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
    "xvi": 16,
    "xvii": 17,
    "xviii": 18,
    "xix": 19,
    "xx": 20,
}


@dataclass
class TomeMatchResult:
    """Résultat de l'analyse d'une annonce pour une série multi-tomes."""

    is_lot: bool = False
    tome_number: int | None = None
    lot_tomes: list[int] = field(default_factory=list)
    lot_count: int | None = None
    matched_subtitle: str | None = None
    confidence: str = "none"  # "high", "medium", "low", "none"
    composition_inferred: bool = False
    """Vrai si `lot_tomes` a été composé par nous et non énoncé par le vendeur.

    Un lot « de 4 tomes » ne dit pas lesquels : c'est le catalogue qui apporte les
    numéros. L'affichage doit le dire, sinon « (Lot) » ferait passer une déduction
    pour un inventaire que le vendeur a lui-même donné.
    """


class TomeExtractor:
    """Analyseur haute performance en mémoire (< 0.2ms) pour détecter tomes et lots."""

    # Mots-clés annonçant un lot
    LOT_KEYWORDS = [
        "lot",
        "integrale",
        "pack",
        "coffret",
        "collection complete",
        "serie complete",
        "tous les tomes",
    ]

    @classmethod
    def extract(
        cls,
        title: str,
        description: str | None = None,
        series_key: str | None = None,
    ) -> TomeMatchResult:
        """Détecte si l'annonce correspond à un tome précis, à un lot ou à la série générale."""
        norm_title = _normalize(title)
        norm_desc = _normalize(description or "")

        # 1. Vérification d'un lot / bundle (prioritaire sur la détection d'un tome unique)
        lot_res = cls._detect_lot(norm_title, norm_desc)
        if lot_res.is_lot:
            return lot_res

        # 2. Recherche explicite d'un tome dans le titre
        tome_num = cls._detect_tome_number(norm_title)
        if tome_num is not None:
            return TomeMatchResult(
                is_lot=False,
                tome_number=tome_num,
                confidence="high",
            )

        # 3. Recherche par sous-titre de série dans le titre
        if series_key:
            norm_key = _normalize(series_key)
            sub_map = SERIES_SUBTITLES.get(norm_key)
            if not sub_map:
                # Tentative de match partiel sur la clé (ex: "1629 — Les naufragés..." -> "1629")
                for k, v in SERIES_SUBTITLES.items():
                    if k in norm_key:
                        sub_map = v
                        break

            if sub_map:
                for sub, t_num in sub_map.items():
                    words = [re.escape(w) for w in sub.split()]
                    pattern = r"\b" + r"[\s,;:\-—]+".join(words) + r"\b"
                    if re.search(pattern, norm_title):
                        return TomeMatchResult(
                            is_lot=False,
                            tome_number=t_num,
                            matched_subtitle=sub,
                            confidence="high",
                        )
                    if norm_desc and re.search(pattern, norm_desc):
                        return TomeMatchResult(
                            is_lot=False,
                            tome_number=t_num,
                            matched_subtitle=sub,
                            confidence="medium",
                        )

            # 3.1 Détection du Tome 1 éponyme quand le 1er album porte le titre de la série
            # (ex: "Le Combat Ordinaire" où "Tome 1" n'est pas imprimé sur la couverture)
            eponymous_series = {"le combat ordinaire"}
            if (
                any(ep in norm_key for ep in eponymous_series)
                and norm_key in norm_title
                and not re.search(r"\b(?:tome|t|vol|livre)\s*\.?\s*([2-9]|\d{2,})\b", norm_title)
                and not re.search(r"\b(?:tome|t|vol|livre)\s*\.?\s*([2-9]|\d{2,})\b", norm_desc)
            ):
                return TomeMatchResult(
                    is_lot=False,
                    tome_number=1,
                    matched_subtitle="Éponyme (Tome 1)",
                    confidence="medium",
                )

        # 4. Recherche de repli dans la description (confiance medium)
        desc_tome = cls._detect_tome_number(norm_desc)
        if desc_tome is not None:
            return TomeMatchResult(
                is_lot=False,
                tome_number=desc_tome,
                confidence="medium",
            )

        # 5. Aucun tome spécifique trouvé
        return TomeMatchResult(is_lot=False, tome_number=None, confidence="none")

    @classmethod
    def _detect_lot(cls, title: str, desc: str = "") -> TomeMatchResult:
        """Détecte les motifs de lots (ex: '1.2.3', 'Lot tomes 1 à 3', 'T1 et T2', '1-3', etc.)."""
        search_texts = [title]
        if desc:
            search_texts.append(desc)

        for text in search_texts:
            # A. Séquences de tomes énumérés avec séparateurs (points, virgules, tirets, etc.)
            # Ex: "1.2.3", "1. 2. 3", "1,2,3", "1, 2, 3", "1-2-3", "1/2/3", "t1-2-3"
            seq_match = re.search(
                r"(?:^|[\s,;:(])(?:tomes?|t\.?|vols?|volumes?)?\s*([0-9]{1,2}(?:\s*(?:[.,\-/+&]|et)\s*[0-9]{1,2}){1,9})(?:$|[\s,;:!?)])",
                text,
            )
            if seq_match:
                raw_seq = seq_match.group(1)
                parts = [
                    p.strip() for p in re.split(r"\s*(?:[.,\-/+&]|et)\s*", raw_seq) if p.strip()
                ]
                if len(parts) >= 2:
                    try:
                        nums = [int(p) for p in parts]
                        # Disambiguation: "2/8", "vol 2/8", "tome 1/5" est une fraction
                        # (Tome X sur Y), PAS un lot
                        if len(nums) == 2 and "/" in raw_seq:
                            pass
                        elif all(1 <= n <= 50 for n in nums):
                            is_decimal = (
                                len(nums) == 2
                                and any(sep in raw_seq for sep in [".", ","])
                                and (
                                    nums[1] >= 10
                                    or "t" not in text[: seq_match.start() + 5].lower()
                                )
                            )
                            if len(nums) >= 3 or not is_decimal:
                                if (
                                    len(nums) == 2
                                    and "-" in raw_seq
                                    and nums[0] < nums[1]
                                    and (nums[1] - nums[0]) <= 15
                                ):
                                    tome_list = list(range(nums[0], nums[1] + 1))
                                else:
                                    tome_list = sorted(set(nums))
                                return TomeMatchResult(
                                    is_lot=True,
                                    lot_tomes=tome_list,
                                    lot_count=len(tome_list),
                                    confidence="high",
                                )
                    except ValueError:
                        pass

            # B. Plage de tomes (ex: 'tomes 1 a 4', '1 a 3', '1 à 3', '1 au 3', '1-3')
            range_match = re.search(
                r"(?:^|[\s,;:(])(?:tomes?|t\.?|vols?|volumes?)?\s*([0-9]{1,2})\s*(?:a|au|à|\-|et)\s*(?:t\.?|tomes?)?\s*([0-9]{1,2})(?:$|[\s,;:!?)])(?!\s*(?:€|eur|euros?|ct|cts|%|cm|mm|kg|g|ans?|mois))",
                text,
            )
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                if 1 <= start < end <= 30 and (end - start) <= 15:
                    tomes = list(range(start, end + 1))
                    return TomeMatchResult(
                        is_lot=True,
                        lot_tomes=tomes,
                        lot_count=len(tomes),
                        confidence="high",
                    )

            # C. Énumération explicite avec préfixe : ex 't1 t2 t3' ou 'tome 1, 2, 3'
            enum_matches = re.findall(r"\b(?:t|tome)\s*([0-9]{1,2})\b(?!\s*(?:€|eur|euros?))", text)
            if len(enum_matches) >= 2:
                try:
                    tome_list = sorted({int(m) for m in enum_matches})
                    if len(tome_list) >= 2 and max(tome_list) <= 30:
                        return TomeMatchResult(
                            is_lot=True,
                            lot_tomes=tome_list,
                            lot_count=len(tome_list),
                            confidence="high",
                        )
                except Exception:
                    pass

            # D. Mot-clé 'lot' dans le texte avec nombre de tomes (ex: 'lot 3 bd', 'lot de 4')
            lot_count_match = re.search(
                r"\blot\s*(?:de)?\s*([0-9]{1,2})\s*(?:bd|mangas?|livres?|tomes?)?\b",
                text,
            )
            if lot_count_match:
                count = int(lot_count_match.group(1))
                if 2 <= count <= 30:
                    return TomeMatchResult(is_lot=True, lot_count=count, confidence="high")

            # E. Nombre explicite de tomes/volumes (ex: '(3 volumes)', '3 tomes')
            vol_count_match = re.search(
                r"(?:\b(?:lot\s*(?:de)?\s*)?|[(\[])([0-9]{1,2})\s*(?:volumes?|vols?|tomes?|bd|livres?)\b(?!\s*(?:€|eur|euros?|ct|cts|%))",
                text,
            )
            if vol_count_match:
                count = int(vol_count_match.group(1))
                if 2 <= count <= 30:
                    return TomeMatchResult(is_lot=True, lot_count=count, confidence="high")

            # F. Mot-clé générique de lot
            for kw in cls.LOT_KEYWORDS:
                if re.search(rf"\b{re.escape(kw)}\b", text):
                    return TomeMatchResult(is_lot=True, confidence="medium")

        return TomeMatchResult(is_lot=False)

    @classmethod
    def _detect_tome_number(cls, text: str) -> int | None:
        """Extrait un numéro de tome unitaire à partir d'un texte normalisé."""
        # 0. Format fraction de tome (ex: 'vol 2/8', 'tome 1/5', 't2/8', 't. 2/8', 'rip 2/6')
        m_frac = re.search(
            r"(?:(?:\b(?:tomes?|t\.?|vols?|volumes?|livre)\s*\.?\s*)|\bt)([0-9]{1,2})\s*\/\s*([0-9]{1,2})\b",
            text,
        )
        if m_frac:
            num = int(m_frac.group(1))
            total = int(m_frac.group(2))
            if 1 <= num <= total <= 50:
                return num

        m_end_frac = re.search(r"\b([0-9]{1,2})\s*\/\s*([0-9]{1,2})\s*$", text)
        if m_end_frac:
            num = int(m_end_frac.group(1))
            total = int(m_end_frac.group(2))
            if 1 <= num < total <= 20:
                return num

        # 1. Mot clé tome/volume : 'tome 3', 't3', 't.3', 'vol 3', 'livre 3'
        m = re.search(
            r"\b(?:tome|t|vol|volume|livre)\s*\.?\s*([0-9]{1,2})\b(?!\s*(?:€|eur|euros?|ct|cts|%))",
            text,
        )
        if m:
            num = int(m.group(1))
            if 1 <= num <= 50:
                return num

        # 2. Format dièse : '#3', '# 3'
        m_hash = re.search(r"#\s*([0-9]{1,2})\b", text)
        if m_hash:
            num = int(m_hash.group(1))
            if 1 <= num <= 50:
                return num

        # 3. Chiffres romains : 'tome iii', 't. iv', 'volume ii'
        m_roman = re.search(
            r"\b(?:tome|t|vol|volume|livre)\s*\.?\s*([ivx]{1,4})\b",
            text,
        )
        if m_roman:
            roman_str = m_roman.group(1)
            if roman_str in ROMAN_NUMERALS:
                return ROMAN_NUMERALS[roman_str]

        # 4. Format tirets/cadratins : '-6-', '- 6 -', '—6—' (très courant en BD)
        m_hyphen = re.search(r"[-_—–]\s*([0-9]{1,2})\s*[-_—–]", text)
        if m_hyphen:
            num = int(m_hyphen.group(1))
            if 1 <= num <= 50:
                return num

        # 5. Format parenthèses ou crochets isolés : '(6)', '[6]'
        m_paren = re.search(r"(?:^|[\s])(?:\(|\[)\s*([0-9]{1,2})\s*(?:\)|\])(?:$|[\s])", text)
        if m_paren:
            num = int(m_paren.group(1))
            if 1 <= num <= 50:
                return num

        # 6. Format suffixe direct : 'rip 2' ou 'blacksad 3' (si seul à la fin)
        m_end = re.search(r"\b([1-9]|1[0-9]|20)\s*$", text)
        if m_end:
            # Ne pas matcher si précédé d'un séparateur de séquence (ex: "1.2.3")
            if re.search(r"[.,\-/+&]\s*\d+\s*$", text):
                return None
            num = int(m_end.group(1))
            if not re.search(rf"(?:format|taille|dim|cm|page|pages)\s*{num}$", text):
                return num

        return None
