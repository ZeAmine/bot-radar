"""Site des Nouveautés Éditeurs simulé, et sa page de résultats.

Le site réel n'est pas interrogeable depuis la suite de tests : deux fichiers la
traversent (le fournisseur seul, puis l'admission dans le service), et deux copies
des mêmes pages finiraient par répondre autre chose. Les gabarits suivent la
structure mesurée le 30/10/2026 (`docs/evaluations/nouveautes-editeurs-2026-10-30.md`)
et **le vrai parseur les relit** : un gabarit qui casserait la lecture casserait
les tests, ce qui est le but.

Le formulaire de recherche n'existe pas sous cette forme : le site tient l'état du
filtre en session. `NouveautesSite` reproduit donc les deux réponses observées —
la liste filtrée quand la session a été amorcée, la liste par défaut sinon — et
compte les requêtes, pour prouver qu'aucune recherche ne dépasse son budget.

Il ne collecte rien (`python_files = ["test_*.py"]`).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date

from app.providers.nouveautes_editeurs import BASE_URL

# Nombre de déclarations de la page par défaut du site, constaté sans session
# amorcée : la recherche n'a pas porté, et l'appelant doit le voir.
DEFAULT_PAGE_SIZE = 20
DEFAULT_RESULT_COUNT = 284

DECLARATION_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head><title>Nouveautés Éditeurs - Accueil - {title} - {publisher}</title></head>
<body>
<div class="detail">
	<input type="hidden" id="dateId" name="dateId" value="{date}"/>
	<input type="hidden" id="unAuteur0" name="unAuteur0" value="{author}"/>
	<input type="hidden" id="collID" name="collID" value="{collection}" />
	<h1>{title}</h1>
	<p>
		Collection / Série :
		<span>
			<a href="#" title="{collection}"
			onclick="addSearchFilter(&#39;collID&#39;,&quot;COLLECTION&quot;);">
				{collection}
			</a>
		</span>
	</p>
	<p>Éditeur : <span><a href="#" title="{publisher}">{publisher}</a></span></p>
	{price_block}
	<div class="type divFlex couleurIconText">
		<img src="ne_images/ne_livre.svg" alt="icône livre" title="Livre"/>
		{document_type}
	</div>
	<p>{format_line}</p>
	{isbn_block}
	{code_block}
	<div class="col1">
		<div class="depot divFlex">
			<span class="span01">
				Dépôt légal
				<img class="svgTraitDetail" src="ne_images/ne_rectangle.svg"
					aria-hidden="true" alt="" title="{deposit}"/>
			<span>{deposit}</span>
			</span>
		</div>
	</div>
</div>
</body>
</html>
"""

SEARCH_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<script>
	var searchState = {{"filtersList":[{filter_entry}],"page":1}};
</script>
</head>
<body>
<div class="titreResultat"><span>{count_text}</span></div>
<div class="grille">
{items}
</div>
</body>
</html>
"""

SEARCH_ITEM = (
    '<div class="vignette"><a href="/accueil?id_declaration={declaration_id}'
    '&amp;titre_livre={slug}" title="{title}">{title}</a></div>'
)


@dataclass(frozen=True)
class SiteDeclaration:
    """Une déclaration servie par la fiche, en champs bruts du site."""

    declaration_id: str
    title: str
    date: str  # jj/mm/aaaa, comme la fiche l'écrit
    author: str = "Whoogy's"
    publisher: str = "Hachette-Livre Editions du Chêne"
    collection: str = "Gastronomie"
    isbn: str | None = "978-2-8123-2249-5"  # forme imprimée, tirets compris
    ean: str | None = "9782812322495"
    price: str | None = "35.00"
    format_line: str = "272 pages ; 26,4 x 21 cm ; broché"
    deposit: str = "Reçu par la BnF"
    document_type: str = "Livre imprimé"

    @property
    def slug(self) -> str:
        return re.sub(r"\W+", "_", self.title).strip("_")

    def page(self) -> str:
        return DECLARATION_PAGE.format(
            title=self.title,
            author=self.author,
            publisher=self.publisher,
            collection=self.collection,
            date=self.date,
            price_block=(
                f"<p>Prix de vente au public (TTC) : {self.price}€</p>"
                if self.price
                else "<p></p>"
            ),
            document_type=self.document_type,
            format_line=self.format_line,
            isbn_block=(
                f"<p><strong>ISBN </strong> \n\t\t\t\t\t\t{self.isbn}\n\t\t\t\t\t</p>"
                if self.isbn
                else ""
            ),
            code_block=(
                f"<p><strong>EAN </strong> \n\t\t\t\t\t\t{self.ean}\n\t\t\t\t\t</p>"
                if self.ean
                else ""
            ),
            deposit=self.deposit,
        )


def search_page(
    declarations: list[SiteDeclaration],
    *,
    count: int | None = None,
    filter_applied: bool = True,
) -> str:
    """Page de résultats : compteur affiché, vignettes, et état du filtre.

    `filter_applied=False` fabrique la réponse d'une session non amorcée : le site
    enregistre le filtre mais sert sa liste par défaut. C'est cette réponse-là qui
    ne doit jamais passer pour une absence.
    """
    total = len(declarations) if count is None else count
    return SEARCH_PAGE.format(
        filter_entry='{"category":"FULL_TEXT"}' if filter_applied else "",
        count_text=f"{total} résultats",
        items="\n".join(
            SEARCH_ITEM.format(
                declaration_id=declaration.declaration_id,
                slug=declaration.slug,
                title=declaration.title,
            )
            for declaration in declarations
        ),
    )


@dataclass
class NouveautesSite:
    """Le site, simulé : deux pages, un compteur de requêtes, aucune surprise.

    `results` associe un terme de recherche aux déclarations que le site renvoie ;
    un terme absent renvoie zéro déclaration, ce qui est une **absence** réelle
    quand le filtre est visible. `requests` garde chaque requête reçue, dans
    l'ordre : le budget du fournisseur se vérifie là, pas dans son code.
    """

    declarations: dict[str, SiteDeclaration] = field(default_factory=dict)
    results: dict[str, list[str]] = field(default_factory=dict)
    applied: bool = True
    status: int = 200
    broken_detail: bool = False
    requests: list[str] = field(default_factory=list)

    def with_declaration(self, declaration: SiteDeclaration, *terms: str) -> NouveautesSite:
        """Ajoute une déclaration, et les termes de recherche qui la renvoient."""
        self.declarations[declaration.declaration_id] = declaration
        for term in terms:
            self.results.setdefault(term, []).append(declaration.declaration_id)
        return self

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def respond(self, request) -> object:
        """Réponse à une requête du fournisseur, ou exception nommée du transport."""
        import httpx

        self.requests.append(f"{request.method} {request.url}")
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "refus simulé"})
        if request.method == "GET" and request.url.path == "/":
            return httpx.Response(200, text="<html><body>accueil</body></html>")
        if request.method == "POST":
            term = self._term(request.content)
            return httpx.Response(200, text=self._search(term))
        declaration_id = request.url.params.get("id_declaration")
        declaration = self.declarations.get(declaration_id or "")
        if declaration is None or self.broken_detail:
            return httpx.Response(200, text="<html><body>fiche inconnue</body></html>")
        return httpx.Response(200, text=declaration.page())

    def url_of(self, declaration_id: str) -> str:
        return f"{BASE_URL}/accueil?id_declaration={declaration_id}"

    @staticmethod
    def _term(body: bytes) -> str:
        form = urllib.parse.parse_qs(body.decode())
        return (form.get("refineValue") or [""])[0]

    def _search(self, term: str) -> str:
        if not self.applied:
            # Session non amorcée : la liste complète du site, filtre pourtant
            # enregistré. Un fournisseur qui lit cela comme une absence se trompe.
            return search_page(
                [
                    SiteDeclaration(
                        declaration_id=f"98{index:012d}",
                        title=f"Parution {index}",
                        date="01/01/2026",
                    )
                    for index in range(DEFAULT_PAGE_SIZE)
                ],
                count=DEFAULT_RESULT_COUNT,
                filter_applied=False,
            )
        found = [
            self.declarations[declaration_id]
            for declaration_id in self.results.get(term, [])
            if declaration_id in self.declarations
        ]
        return search_page(found)


def today_string(offset_days: int = 0) -> str:
    """Une date de fiche, décalée depuis aujourd'hui — jamais une date figée."""
    from datetime import timedelta

    return (date.today() + timedelta(days=offset_days)).strftime("%d/%m/%Y")
