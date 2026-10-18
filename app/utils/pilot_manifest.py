import hashlib
import json
from typing import Any

from app.utils.isbn import normalize_listing_isbn


def digest(manifest: dict[str, Any]) -> str:
    content = {key: value for key, value in manifest.items() if key != "digest"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def validate_manifest(manifest: dict[str, Any], expected: str) -> None:
    codes = manifest.get("collection_isbn13", [])
    if (manifest.get("scope") != "pilot_only" or manifest.get("digest") != expected
            or digest(manifest) != expected or len(codes) != 7 or len(set(codes)) != 7
            or manifest.get("maximum_unique_collections") != 7
            or any(normalize_listing_isbn(code) != code for code in codes)):
        raise ValueError("Manifeste pilote invalide")
    references = [book.get("isbn13") for book in manifest.get("books", [])]
    references += [repair.get("isbn13") for repair in manifest.get("catalogue_repairs", [])]
    references += [group.get("isbn13") for group in manifest.get("isbn_aliases", [])]
    if any(normalize_listing_isbn(code) not in codes for code in references):
        raise ValueError("Référence hors manifeste")
