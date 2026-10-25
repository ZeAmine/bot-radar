"""Rejeu des captures CAL du 21 octobre 2026 : signal de complétude et sondage.

Les réponses viennent de deux HAR réels. La règle démontrée est que la collecte
d'offres est complète quand l'ensemble des moteurs demandés a répondu : un moteur
qui a répondu sans rien trouver est présent avec une liste vide, un moteur muet
est absent. Le sondage renvoie à chaque tour les seuls moteurs encore attendus.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers.base import requested_components
from app.providers.chasse_aux_livres import (
    LOOKUP_POLL_MAX,
    ChasseAuxLivresProvider,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/chasse"
BASE = "https://www.chasse-aux-livres.fr"

JEAN_DOUX = {
    "isbn13": "9782756079332",
    "item_id": "2756079332",
    "path": "/prix/2756079332/9782756079332",
    "fixture": "lookup_jean_doux_2026_10_21.json",
}
NANBANJIN = {
    "isbn13": "9782808500487",
    "item_id": "2808500483",
    "path": "/prix/2808500483/9782808500487",
    "fixture": "lookup_nanbanjin_2026_10_21.json",
}


def _product_page(item_id: str, nbeng: int) -> str:
    return (
        f'<div data-asin="{item_id}" data-lvs="synthetic" '
        f'data-nbeng="{nbeng}" data-didh="abc123"></div>'
    )


def _capture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def _replay(book: dict, nbeng: int = 32) -> tuple:
    capture = _capture(book["fixture"])
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(book["item_id"], nbeng))
        lookups.append(request)
        retry = request.url.params.get("retry", "0")
        return httpx.Response(200, json=capture["responses"][retry])

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=book["isbn13"],
                title="Ouvrage",
                source_url=BASE + book["path"],
            )
    finally:
        requested_components.reset(scope)
    return result, lookups


@pytest.mark.asyncio
async def test_jean_doux_capture_is_complete_without_polling():
    result, lookups = await _replay(JEAN_DOUX)

    attempt = result.attempts[0]
    assert attempt.status == "success"
    assert attempt.reason is None
    assert attempt.engines_requested == 32
    assert attempt.engines_received == 32
    assert attempt.poll_count == 0
    assert len(lookups) == 1

    offer = result[0]
    assert offer.delivered_price_cents == 1628
    assert offer.raw_payload["best_merchant"] == "Recyclivre"
    assert offer.raw_payload["partners"][offer.raw_payload["best_merchant"]][
        "total_cents"
    ] == 1628
    assert len(offer.raw_payload["partners"]) == 8
    assert offer.raw_payload["collection_status"] == "success"
    assert offer.raw_payload["engines_received"] == 32
    assert offer.raw_payload["poll_count"] == 0


@pytest.mark.asyncio
async def test_nanbanjin_capture_polls_only_the_missing_engines():
    result, lookups = await _replay(NANBANJIN)

    attempt = result.attempts[0]
    assert attempt.status == "success"
    assert attempt.engines_requested == 32
    assert attempt.engines_received == 32
    assert attempt.poll_count == 3
    assert attempt.reason is None

    assert [(r.url.params["retry"], r.url.params["engines"]) for r in lookups] == [
        ("0", "_".join(str(i) for i in range(32))),
        ("1", "0_1_22_30_31"),
        ("2", "1_30_31"),
        ("3", "30_31"),
    ]


@pytest.mark.asyncio
async def test_nanbanjin_complete_without_comparable_offer_is_a_confirmed_absence():
    result, _ = await _replay(NANBANJIN)

    assert list(result) == []
    assert result.attempts[0].status == "success"
    assert result.failed_providers == ()
    assert not result.historical_checked


@pytest.mark.asyncio
async def test_pending_engines_after_the_bound_never_assert_absence():
    partial = _capture("partial_offers_2026_10_19.json")["payload"]
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 25))
        lookups.append(request)
        return httpx.Response(200, json=partial)

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.status == "partial"
    assert attempt.reason == "incomplete_offers"
    assert attempt.engines_requested == 25
    assert attempt.engines_received == 4
    assert attempt.poll_count == LOOKUP_POLL_MAX
    assert len(lookups) == 1 + LOOKUP_POLL_MAX
    assert result.failed_providers == ("chasse_aux_livres",)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [JEAN_DOUX, NANBANJIN])
async def test_incomplete_pending_engines_never_expose_a_successful_collection(book):
    partial = _capture("partial_offers_2026_10_19.json")["payload"]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(book["item_id"], 32))
        return httpx.Response(200, json=partial)

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=book["isbn13"],
                title="Ouvrage",
                source_url=BASE + book["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.engines_received < attempt.engines_requested
    assert attempt.status == "partial"
    for offer in result:
        assert offer.raw_payload["collection_status"] == "partial"
        assert offer.raw_payload["engines_received"] < offer.raw_payload["engines_requested"]


@pytest.mark.asyncio
async def test_product_page_without_engines_is_a_technical_failure():
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 0))
        lookups.append(request)
        return httpx.Response(200, json={"offers": {}})

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    assert result.attempts[0].status == "failed"
    assert result.attempts[0].reason == "invalid_response"
    assert lookups == []


@pytest.mark.asyncio
async def test_empty_poll_body_stops_as_partial_without_absence():
    offer = {
        "logo": {"name": "MOMOX_SHOP"},
        "condition": {"_name": "USED"},
        "subCondition": {"name": "Très bon"},
        "price": {"amount": 1200},
        "shippingCost": {"amount": 250},
        "totalPrice": {"amount": 1450},
        "affiliateUrl": "https://merchant.example/offer",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 2))
        if request.url.params.get("retry") == "0":
            return httpx.Response(200, json={"offers": {"0": [offer]}})
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.status == "partial"
    assert attempt.poll_count == 1
    assert attempt.engines_received == 1
    assert result[0].delivered_price_cents == 1450
    assert result[0].raw_payload["collection_status"] == "partial"
    assert all(offer.item_condition != "Occasion indisponible" for offer in result)


@pytest.mark.asyncio
async def test_confirmed_absence_still_attempts_history_before_returning(monkeypatch):
    """Un current complet sans offre ne doit pas court-circuiter l'historique.

    L'historique est collecté dans `_try_deep_lookup`, donc avant le retour
    anticipé de `get_offers`. Ce test le verrouille : current complet sans offre
    comparable, historique demandé mais en échec, et aucune recherche par titre.
    """
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.resolve_session_token",
        AsyncMock(return_value="synthetic-session"),
    )
    history_call = AsyncMock(return_value=httpx.Response(500))
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.chasse_session.get_history", history_call,
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(200, text=(
                '<div id="hash-cont" data-hash="mockhash1234567890abcdef12345678"></div>'
            ))
        if request.url.path == "/rest/search-results":
            return httpx.Response(200, json={
                "d": '<a href="/prix/2756079332/9782756079332">Jean Doux</a>',
            })
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" '
                'data-nbeng="2" data-didh="abc123"></div>'
            ))
        return httpx.Response(200, json={"offers": {"0": [], "1": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(
            http_client=client, sleep=AsyncMock(),
        ).get_offers(isbn13="9782756079332", title="Jean Doux")

    assert history_call.await_count == 1
    assert requests.count("/search") == 1
    assert [(attempt.operation, attempt.status) for attempt in result.attempts] == [
        ("current", "success"),
        ("history", "failed"),
    ]
    assert result.historical_checked is False


async def _diagnostic_replay(
    monkeypatch, replies, *, latency=0.05, oversleep=0.0, nbeng=3, product_reply=None,
):
    from types import SimpleNamespace

    from app.providers import chasse_aux_livres as cal

    ticks = [0.0]
    clock_reads = []
    requests = []
    waits = []

    def monotonic():
        clock_reads.append(ticks[0])
        return ticks[0]

    async def sleep(delay):
        waits.append(delay)
        ticks[0] += delay + oversleep

    def respond(request):
        requests.append((
            str(request.url), request.extensions.get("timeout"),
            request.method, dict(request.headers),
        ))
        if request.url.path.startswith("/prix/"):
            if isinstance(product_reply, BaseException):
                raise product_reply
            if product_reply is not None:
                return product_reply
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], nbeng))
        index = sum("/rest/lookup/results" in entry[0] for entry in requests) - 1
        ticks[0] += latency
        reply = replies[min(index, len(replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(cal, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(cal, "perf_counter", lambda: ticks[0])
    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await cal.ChasseAuxLivresProvider(
                http_client=client, sleep=sleep,
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"], title="Ouvrage",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)
    return result, requests, waits, clock_reads


def _lookup_records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "cal_lookup_response"]


@pytest.mark.asyncio
async def test_lookup_trace_records_progress_and_client_completion(monkeypatch, caplog):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, [
        {"offers": {"0": []}}, {"offers": {"1": []}}, {"offers": {"2": []}},
    ])
    records = _lookup_records(caplog)
    assert len(records) == 3
    assert len({r.trace_id for r in records}) == 1
    assert [r.poll_index for r in records] == [0, 1, 2]
    assert [r.client_received_groups for r in records] == [["0"], ["0", "1"], ["0", "1", "2"]]
    assert [r.new_group_ids for r in records] == [["0"], ["1"], ["2"]]
    assert [r.client_decision for r in records] == ["continue", "continue", "stop"]
    assert [r.client_reason for r in records] == [
        "pending_groups", "pending_groups", "client_groups_present",
    ]
    assert all(r.source_metadata == {} for r in records)
    assert all(r.payload_shape == "object" for r in records)
    assert all(r.source_nbeng == 3 and r.client_expected_groups == ["0", "1", "2"]
               for r in records)
    assert [round(r.response_elapsed_ms) for r in records] == [50, 300, 550]
    assert waits == [0.2, 0.2]
    assert len(requests) == 4
    assert result.attempts[0].status == "success"
    assert result.attempts[0].poll_count == 2


@pytest.mark.asyncio
async def test_lookup_trace_poll_limit_is_not_source_completion(monkeypatch, caplog):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, [
        {"offers": {}},
        *[{"offers": {str(i): []}} for i in range(LOOKUP_POLL_MAX)],
    ], nbeng=40)
    records = _lookup_records(caplog)
    assert len(records) == LOOKUP_POLL_MAX + 1
    assert records[-1].new_group_ids == [str(LOOKUP_POLL_MAX - 1)]
    assert records[-1].client_decision == "stop"
    assert records[-1].client_reason == "poll_limit"
    assert records[-1].source_nbeng == 40
    assert len(records[-1].client_expected_groups) == 32
    assert records[-1].source_metadata == {}
    assert waits == [0.2] * LOOKUP_POLL_MAX
    assert len(requests) == LOOKUP_POLL_MAX + 2
    assert result.attempts[0].status == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize(("latency", "oversleep", "reason", "waits"), [
    (4.9, 0.0, "budget_before_sleep", []),
    (0.05, 4.8, "budget_after_sleep", [0.2]),
])
async def test_lookup_trace_budget_branch_is_the_executed_branch(
    monkeypatch, caplog, latency, oversleep, reason, waits,
):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, actual_waits, _ = await _diagnostic_replay(
        monkeypatch, [{"offers": {}}], latency=latency, oversleep=oversleep,
    )
    records = _lookup_records(caplog)
    assert len(records) == 1
    assert records[0].client_decision == "stop"
    assert records[0].client_reason == reason
    assert len(requests) == 2
    assert actual_waits == waits
    assert result.attempts[0].status == "partial"
    assert result.attempts[0].poll_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "reason", "status"), [
    (httpx.Response(200, content=b""), "empty_poll_body", "partial"),
    ({"offers": []}, "poll_offers_not_object", "partial"),
    ([], "poll_offers_not_object", "partial"),
    (httpx.Response(200, content=b"not json"), "poll_exception", "partial"),
    (httpx.ReadTimeout("private detail"), "poll_exception", "partial"),
    (httpx.Response(500), "poll_response_failure", "partial"),
    (httpx.Response(429), "poll_source_restriction", "failed"),
    (httpx.Response(200, headers={"cf-mitigated": "challenge"}),
     "poll_source_restriction", "failed"),
])
async def test_lookup_trace_preserves_poll_failure_outcomes(
    monkeypatch, caplog, reply, reason, status,
):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(
        monkeypatch, [{"offers": {"0": []}}, reply],
    )
    records = _lookup_records(caplog)
    assert len(records) == 2
    assert records[0].client_decision == "continue"
    assert records[-1].client_reason == reason
    assert records[-1].client_decision == "stop"
    assert records[-1].response_received is not isinstance(reply, Exception)
    if isinstance(reply, (httpx.Response, Exception)):
        assert records[-1].payload_shape == "not_parsed"
        assert not hasattr(records[-1], "source_offer_group_ids")
    assert records[-1].client_received_groups == ["0"]
    assert len(requests) == 3
    assert waits == [0.2]
    assert result.attempts[0].status == status


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "reason"), [
    ([], "initial_payload_not_object"),
    ({"offers": []}, "initial_offers_not_object"),
    (httpx.Response(200, content=b"not json"), "initial_json_error"),
    (httpx.ReadTimeout("private detail"), "initial_request_exception"),
    (httpx.Response(500), "initial_http_error"),
    (httpx.Response(429), "initial_source_restriction"),
    (httpx.Response(200, headers={"cf-mitigated": "challenge"}),
     "initial_source_restriction"),
])
async def test_lookup_trace_preserves_initial_failure_outcomes(monkeypatch, caplog, reply, reason):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, [reply])
    records = _lookup_records(caplog)
    assert len(records) == 1
    assert records[0].client_reason == reason
    assert records[0].client_decision == "stop"
    assert records[0].response_received is not isinstance(reply, Exception)
    assert records[0].payload_shape == ("non_object" if isinstance(reply, list) else (
        "object" if isinstance(reply, dict) else "not_parsed"
    ))
    assert len(requests) == 2
    assert waits == []
    assert result.attempts[0].status == "failed"


@pytest.mark.asyncio
async def test_lookup_trace_does_not_interpret_or_publish_source_metadata(monkeypatch, caplog):
    from app.core.logging import JSONFormatter

    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    payload = {
        "offers": {"0": [], "PRIVATE_ACCOUNT_GROUP": []},
        "done": True, "pending": None,
        "status": "PRIVATE_COOKIE_VALUE", "progress": {"account": "PRIVATE_PERSON"},
        "Authorization": "PRIVATE_BEARER", "PRIVATE_PERSON@example.invalid": True,
    }
    result, _, _, _ = await _diagnostic_replay(monkeypatch, [payload])
    records = _lookup_records(caplog)
    assert len(records) == LOOKUP_POLL_MAX + 1
    for row in records:
        assert row.source_metadata == {
            "done": {"type": "boolean", "interpretation": "unknown", "value": True},
            "pending": {"type": "null", "interpretation": "unknown", "value": None},
            "status": {"type": "string", "interpretation": "unknown"},
            "progress": {"type": "object", "interpretation": "unknown"},
        }
        assert row.source_offer_group_ids == ["0"]
        assert row.unrequested_groups_count == 1
        assert row.source_metadata_unreported_count == 2
    assert records[-1].new_group_ids == []
    assert records[-1].client_reason == "poll_limit"
    assert result.attempts[0].status == "partial"
    formatter = JSONFormatter()
    for row in caplog.records:
        if getattr(row, "event", None) in {"cal_product_response", "cal_lookup_response"}:
            encoded = formatter.format(row)
            assert "PRIVATE_" not in encoded
            assert "example.invalid" not in encoded
            assert "synthetic" not in encoded
            assert "9782756079332" not in encoded
            assert len(encoded) < 8000


@pytest.mark.asyncio
async def test_lookup_trace_logging_failure_does_not_change_collection(monkeypatch):
    from app.providers import chasse_aux_livres as cal

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(cal.logger, "info", fail_log)
    result, requests, waits, _ = await _diagnostic_replay(
        monkeypatch, [{"offers": {"0": [], "1": [], "2": []}}],
    )
    assert result.attempts[0].status == "success"
    assert result.attempts[0].poll_count == 0
    assert len(requests) == 2
    assert waits == []


@pytest.mark.asyncio
async def test_lookup_trace_does_not_change_cancellation(monkeypatch, caplog):
    import asyncio

    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    with pytest.raises(asyncio.CancelledError):
        await _diagnostic_replay(monkeypatch, [asyncio.CancelledError()])
    records = _lookup_records(caplog)
    assert len(records) == 1
    assert not records[0].response_received
    assert records[0].client_decision == "stop"
    assert records[0].client_reason == "propagated_exception"


@pytest.mark.asyncio
async def test_lookup_trace_does_not_turn_source_metadata_into_a_completion_rule(
    monkeypatch, caplog,
):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, [{
        "offers": {"0": [], "1": [], "2": []}, "done": False,
    }])
    records = _lookup_records(caplog)
    assert len(records) == 1
    assert records[0].source_metadata["done"] == {
        "type": "boolean", "interpretation": "unknown", "value": False,
    }
    assert records[0].client_reason == "client_groups_present"
    assert result.attempts[0].status == "success"
    assert len(requests) == 2
    assert waits == []


@pytest.mark.asyncio
async def test_lookup_trace_product_and_lookup_share_only_a_local_id(monkeypatch, caplog):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    await _diagnostic_replay(monkeypatch, [{"offers": {}}])
    records = [r for r in caplog.records if getattr(r, "event", None) in {
        "cal_product_response", "cal_lookup_response",
    }]
    assert len(records) == LOOKUP_POLL_MAX + 2
    assert len({r.trace_id for r in records}) == 1
    assert len(records[0].trace_id) == 16
    assert records[0].event == "cal_product_response"
    assert records[0].poll_index is None
    assert records[0].client_reason == "product_metadata_accepted"
    assert records[0].client_decision == "continue"
    assert all(r.response_received for r in records)
    caplog.clear()
    await _diagnostic_replay(monkeypatch, [{"offers": {"0": [], "1": [], "2": []}}])
    assert _lookup_records(caplog)[0].trace_id != records[0].trace_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "nbeng", "reason"), [
    (httpx.Response(500), 3, "product_response_failure"),
    (httpx.Response(200, text="<div></div>"), 3, "product_metadata_missing"),
    (httpx.Response(200, text=_product_page(NANBANJIN["item_id"], 3)),
     3, "product_identity_mismatch"),
    (httpx.ReadTimeout("private detail"), 3, "product_request_exception"),
    (None, 0, "no_requested_groups"),
])
async def test_lookup_trace_product_failure_never_claims_a_lookup(
    monkeypatch, caplog, reply, nbeng, reason,
):
    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    result, requests, waits, _ = await _diagnostic_replay(
        monkeypatch, [{"offers": {}}], product_reply=reply, nbeng=nbeng,
    )
    records = [r for r in caplog.records if getattr(r, "event", None) == "cal_product_response"]
    assert len(records) == 1
    assert records[0].poll_index is None
    assert records[0].client_reason == reason
    assert records[0].client_decision == "stop"
    assert records[0].response_received is not isinstance(reply, Exception)
    assert _lookup_records(caplog) == []
    assert len(requests) == 1
    assert waits == []
    assert result.attempts[0].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_poll", [False, True])
async def test_lookup_trace_preserves_the_existing_suspension_path(
    monkeypatch, caplog, during_poll,
):
    from app.providers.cal_governor import CalSuspendedError

    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    replies = [CalSuspendedError("synthetic")]
    if during_poll:
        replies.insert(0, {"offers": {}})
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, replies)
    records = _lookup_records(caplog)
    assert records[-1].client_decision == "stop"
    assert not records[-1].response_received
    assert records[-1].client_reason == ("poll_exception" if during_poll else "initial_suspended")
    assert result.attempts[0].status == ("partial" if during_poll else "skipped")
    assert len(requests) == (3 if during_poll else 2)
    assert waits == ([0.2] if during_poll else [])


@pytest.mark.asyncio
async def test_lookup_trace_never_enters_the_market_payload_or_certifies_partial(monkeypatch):
    from app.providers.registry import ProviderRegistry
    from app.services.book_market_pricing import BookPricingResult

    row = {
        "logo": {"name": "SYNTHETIC_SHOP"}, "condition": {"_name": "USED"},
        "subCondition": {"name": "Très bon"}, "price": {"amount": 1200},
        "shippingCost": {"amount": 250}, "totalPrice": {"amount": 1450},
        "affiliateUrl": "https://example.invalid/offer",
    }
    offers, _, _, _ = await _diagnostic_replay(monkeypatch, [{
        "offers": {"0": [row]}, "done": True,
    }])
    assert len(offers) == 1
    assert offers[0].delivered_price_cents == 1450
    assert offers[0].raw_payload["collection_status"] == "partial"
    assert {"trace_id", "source_metadata", "client_reason", "client_decision"}.isdisjoint(
        offers[0].raw_payload,
    )
    result = BookPricingResult.from_offers(
        offers, ProviderRegistry.select_best_offer(offers), None,
    )
    assert result.lowest_delivered_price_cents is None
    assert not result.price_confirmed
    assert not result.current_collection_usable


@pytest.mark.asyncio
@pytest.mark.parametrize("during_poll", [False, True])
async def test_lookup_trace_identifies_collection_budget_without_changing_outcome(
    monkeypatch, caplog, during_poll,
):
    from app.providers.cal_governor import CalSuspendedError

    caplog.set_level("INFO", logger="radar.providers.chasse_aux_livres")
    replies = [CalSuspendedError("collection_budget")]
    if during_poll:
        replies.insert(0, {"offers": {"0": []}})
    result, requests, waits, _ = await _diagnostic_replay(monkeypatch, replies)
    record = _lookup_records(caplog)[-1]
    assert record.governor_reason == "collection_budget"
    assert record.client_reason == ("poll_exception" if during_poll else "initial_suspended")
    assert record.client_decision == "stop"
    assert record.response_received is False
    assert len(requests) == (3 if during_poll else 2)
    assert waits == ([0.2] if during_poll else [])
    assert result.attempts[0].status == ("partial" if during_poll else "skipped")
