"""Le renouvellement par connexion au compte, testé sans navigateur ni réseau.

La capture Playwright a disparu avec le défi images (ADR-036) : ce qui se
teste ici est le parsing — champs anti-rejeu du formulaire, détection d'un
formulaire encore ouvert (identifiants refusés), lecture de ``data-duih`` —
et la chaîne complète de capture contre un transport factice. La règle de
sélection entre attribut HTML et cookie, elle, reste pure : c'est elle qui
décide si le script renvoie un jeton de session membre ou un cookie d'entrée
sans valeur.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from refresh_chasse_token import capture_token, extract_duih, extract_login_fields  # noqa: E402

LOGIN_PAGE_HTML = """
<html><body>
<form method="post" name="login" action="/login" id="login">
  <input type="text" id="email" name="email"/>
  <input type="password" id="password" name="password"/>
  <div style="display:none;visibility: hidden">
    <input type="text" name="protect" value="true"/>
    <input type="hidden" name="_sourcePage" value="SOURCE-PAGE-123"/>
    <input type="hidden" name="__fp" value="FINGERPRINT-456"/>
  </div>
</form>
</body></html>
"""

MEMBER_HOME_HTML = '<html data-duih="jeton-membre.signature"><body>membre</body></html>'


def _cookie(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def test_the_html_attribute_wins_over_the_cookie():
    """Un cookie `duih` existe dès l'entrée : seul l'attribut signale un jeton confirmé."""
    token = extract_duih(
        "jeton-du-defi",
        [_cookie("duih", "cookie-davant")],
    )

    assert token == "jeton-du-defi"


def test_the_cookie_is_the_fallback_when_the_attribute_is_absent():
    """Le site peut aussi ne rendre l'attribut qu'après coup : le cookie suffit."""
    token = extract_duih(None, [_cookie("duih", "jeton-du-cookie")])

    assert token == "jeton-du-cookie"


def test_an_empty_attribute_falls_back_to_the_cookie():
    """Une chaîne blanche n'est pas un jeton : elle ne doit pas écraser le cookie."""
    token = extract_duih("   ", [_cookie("duih", "jeton-du-cookie")])

    assert token == "jeton-du-cookie"


def test_nothing_found_is_none_not_empty_string():
    """Le script distingue « pas encore » de « trouvé » par un None explicite."""
    assert extract_duih(None, [_cookie("session", "autre")]) is None
    assert extract_duih(None, []) is None


def test_login_fields_are_read_from_the_form():
    """Les deux champs anti-rejeu sont lus tels que la page les rend."""
    assert extract_login_fields(LOGIN_PAGE_HTML) == ("SOURCE-PAGE-123", "FINGERPRINT-456")


def test_missing_login_fields_are_none_not_empty_values():
    """Un formulaire sans `_sourcePage` ou sans `__fp` est inutilisable : on le dit."""
    no_fp = LOGIN_PAGE_HTML.replace('name="__fp" value="FINGERPRINT-456"', 'name="__fp"')
    no_hidden = LOGIN_PAGE_HTML.split('<div style="display:none;visibility: hidden">')[0]

    assert extract_login_fields(no_fp) is None
    assert extract_login_fields(no_hidden) is None


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=5.0,
    )


def test_capture_logs_in_and_reads_the_home_attribute():
    """La chaîne complète : formulaire → POST → accueil membre → data-duih."""
    posted: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE_HTML, request=request)
        if request.url.path == "/login" and request.method == "POST":
            posted["body"] = request.content.decode()
            # Le POST reçoit une redirection vers l'espace membre, comme en réel.
            return httpx.Response(
                302, headers={"Location": "/user/memo"}, request=request
            )
        if request.url.path == "/user/memo":
            return httpx.Response(
                200, text="<html><body>espace membre</body></html>", request=request
            )
        if request.url.path == "/":
            return httpx.Response(200, text=MEMBER_HOME_HTML, request=request)
        return httpx.Response(404, request=request)

    with _client(handler) as client:
        token = capture_token(client, "membre@example.org", "mot-de-passe")

    assert token == "jeton-membre.signature"
    # Le formulaire a bien reçu ses champs anti-rejeu et l'identité soumise.
    assert "SOURCE-PAGE-123" in posted["body"]
    assert "FINGERPRINT-456" in posted["body"]
    assert "membre%40example.org" in posted["body"]


def test_capture_refuses_when_the_form_is_still_open_after_post():
    """Un formulaire rendu intact après POST = identifiants refusés : rien à valider."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(200, text=LOGIN_PAGE_HTML, request=request)
        # Réponse de connexion en échec : la page de connexion revient telle quelle.
        return httpx.Response(200, text=LOGIN_PAGE_HTML, request=request)

    with _client(handler) as client:
        token = capture_token(client, "membre@example.org", "mauvais-mdp")

    assert token is None


def test_capture_stops_before_posting_when_the_form_changed():
    """Sans champs anti-rejeu, la connexion ne tente même pas : formulaire changé."""
    posted_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted_methods.append(request.method)
        bare = LOGIN_PAGE_HTML.split('<div style="display:none;visibility: hidden">')[0]
        return httpx.Response(200, text=bare, request=request)

    with _client(handler) as client:
        token = capture_token(client, "membre@example.org", "mot-de-passe")

    assert token is None
    assert "POST" not in posted_methods


def test_capture_returns_none_when_the_home_has_no_duih():
    """Membre connecté mais aucun jeton rendu : None explicite, pas une chaîne vide."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE_HTML, request=request)
        if request.method == "POST":
            return httpx.Response(
                302, headers={"Location": "/user/memo"}, request=request
            )
        return httpx.Response(200, text="<html><body>accueil</body></html>", request=request)

    with _client(handler) as client:
        token = capture_token(client, "membre@example.org", "mot-de-passe")

    assert token is None
