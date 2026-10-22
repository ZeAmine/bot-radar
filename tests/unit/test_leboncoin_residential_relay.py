"""Tests du relais SOCKS5 résidentiel.

La recette isolée refuse toute connexion réseau réelle, y compris la boucle
locale : le protocole est donc exercé sur des paires de sockets et la seule
sortie (`create_connection`) est doublée. Aucun port n'est ouvert, aucun
paquet ne sort.
"""

import contextlib
import socket
import struct
import threading
import time

import pytest

from scripts.leboncoin_residential_relay import (
    Socks5Server,
    _negotiate,
    _read_request,
    _relay,
)


def _request_domain(host: str, port: int) -> bytes:
    encoded = host.encode("idna")
    header = struct.pack("!BBBB", 5, 1, 0, 3) + bytes([len(encoded)]) + encoded
    return header + struct.pack("!H", port)


def _fake_resolution(address: str):
    def infos(*_args: object, **_kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    return infos


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def _request_ipv4(host: str, port: int) -> bytes:
    return struct.pack("!BBBB", 5, 1, 0, 1) + socket.inet_aton(host) + struct.pack("!H", port)


def test_negotiate_accepts_no_auth():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x05\x01\x00")
        assert _negotiate(server) is True
        assert client.recv(2) == b"\x05\x00"


def test_negotiate_refuses_missing_method():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x05\x01\x02")
        assert _negotiate(server) is False
        assert client.recv(2) == b"\x05\xff"


def test_negotiate_refuses_foreign_version():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x04\x01\x00")
        assert _negotiate(server) is False


def test_read_request_ipv4_and_domain():
    server, client = _pair()
    with server, client:
        client.sendall(_request_ipv4("192.0.2.10", 8080))
        assert _read_request(server) == ("192.0.2.10", 8080)

    server, client = _pair()
    with server, client:
        payload = struct.pack("!BBBB", 5, 1, 0, 3) + b"\x0bexample.com" + struct.pack("!H", 443)
        client.sendall(payload)
        assert _read_request(server) == ("example.com", 443)


def test_read_request_refuses_unknown_command():
    server, client = _pair()
    with server, client:
        client.sendall(struct.pack("!BBBB", 5, 2, 0, 1) + b"\x00" * 6)
        assert _read_request(server) is None
        assert client.recv(10)[1] == 1


def test_relay_copies_bytes_in_both_directions():
    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    thread = threading.Thread(target=_relay, args=(relay_client, relay_upstream), daemon=True)
    thread.start()
    try:
        with client_side, upstream_side, relay_client, relay_upstream:
            client_side.sendall(b"bonjour")
            assert upstream_side.recv(7) == b"bonjour"
            upstream_side.sendall(b"reponse")
            assert client_side.recv(7) == b"reponse"
    finally:
        thread.join(timeout=2.0)


def test_handle_client_full_handshake_over_socketpair(monkeypatch: pytest.MonkeyPatch):
    server_end, client_end = _pair()
    upstream_local, upstream_remote = _pair()
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution("93.184.216.34"))
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: upstream_local)

    server = Socks5Server()
    thread = threading.Thread(
        target=server._handle_client, args=(server_end,), daemon=True
    )
    thread.start()
    try:
        with client_end, upstream_remote:
            client_end.sendall(b"\x05\x01\x00")
            assert client_end.recv(2) == b"\x05\x00"
            client_end.sendall(_request_domain("www.leboncoin.fr", 443))
            reply = client_end.recv(10)
            assert reply[:2] == b"\x05\x00"
            client_end.sendall(b"blacksad")
            assert upstream_remote.recv(8) == b"blacksad"
            upstream_remote.sendall(b"\x00ki")
            assert client_end.recv(3) == b"\x00ki"
    finally:
        thread.join(timeout=2.0)
        server.close()


def test_handle_client_reports_refused_connection(monkeypatch: pytest.MonkeyPatch):
    server_end, client_end = _pair()

    def refuse(*_args: object, **_kwargs: object) -> socket.socket:
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution("93.184.216.34"))
    monkeypatch.setattr(socket, "create_connection", refuse)

    server = Socks5Server()
    thread = threading.Thread(
        target=server._handle_client, args=(server_end,), daemon=True
    )
    thread.start()
    try:
        with client_end:
            client_end.sendall(b"\x05\x01\x00")
            assert client_end.recv(2) == b"\x05\x00"
            client_end.sendall(_request_domain("www.leboncoin.fr", 443))
            assert client_end.recv(10)[1] == 5
    finally:
        thread.join(timeout=2.0)
        server.close()


def test_handle_client_closes_on_foreign_greeting():
    server_end, client_end = _pair()
    server = Socks5Server()
    thread = threading.Thread(
        target=server._handle_client, args=(server_end,), daemon=True
    )
    thread.start()
    try:
        with client_end:
            client_end.sendall(b"GET / HTTP/1.1\r\n\r\n")
            time.sleep(0.05)
            with contextlib.suppress(ConnectionResetError):
                assert client_end.recv(2) == b""
    finally:
        thread.join(timeout=2.0)
        server.close()


PRIVATE_ANSWER = _fake_resolution("10.1.2.3")


def test_allowed_domain_and_subdomains():
    from scripts.leboncoin_residential_relay import DEFAULT_ALLOWED_DOMAINS, domain_is_allowed

    assert domain_is_allowed("leboncoin.fr", DEFAULT_ALLOWED_DOMAINS)
    assert domain_is_allowed("www.leboncoin.fr", DEFAULT_ALLOWED_DOMAINS)
    assert domain_is_allowed("api.leboncoin.fr.", DEFAULT_ALLOWED_DOMAINS)
    assert not domain_is_allowed("evil-leboncoin.fr", DEFAULT_ALLOWED_DOMAINS)
    assert not domain_is_allowed("leboncoin.fr.evil.com", DEFAULT_ALLOWED_DOMAINS)
    assert not domain_is_allowed("example.com", DEFAULT_ALLOWED_DOMAINS)


def test_the_default_allowlist_never_contains_the_exit_check(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    assert "api.ipify.org" not in DEFAULT_ALLOWED_DOMAINS
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution("93.184.216.34"))
    assert resolve_allowed_addresses("api.ipify.org", 443, DEFAULT_ALLOWED_DOMAINS) is None


def test_the_exit_check_stays_usable_as_an_explicit_extra_domain(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution("93.184.216.34"))
    allowed = (*DEFAULT_ALLOWED_DOMAINS, "api.ipify.org")
    assert resolve_allowed_addresses("api.ipify.org", 443, allowed) == ["93.184.216.34"]


def test_a_literal_ipv4_destination_is_refused(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    called: list[object] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **_k: called.append(a))
    assert resolve_allowed_addresses("93.184.216.34", 443, DEFAULT_ALLOWED_DOMAINS) is None
    assert called == [], "aucune résolution pour une adresse littérale"


def test_an_unlisted_domain_is_refused_without_resolution(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    called: list[object] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **_k: called.append(a))
    assert resolve_allowed_addresses("example.com", 443, DEFAULT_ALLOWED_DOMAINS) is None
    assert called == []


def test_a_domain_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    monkeypatch.setattr(socket, "getaddrinfo", PRIVATE_ANSWER)
    assert resolve_allowed_addresses("www.leboncoin.fr", 443, DEFAULT_ALLOWED_DOMAINS) is None


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "224.0.0.1", "0.0.0.0", "::1", "fe80::1"],
)
def test_non_public_addresses_are_refused(address: str):
    from scripts.leboncoin_residential_relay import _is_public_address

    assert not _is_public_address(address)


def test_a_multi_address_domain_with_one_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    def mixed(*_args: object, **_kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    assert resolve_allowed_addresses("www.leboncoin.fr", 443, DEFAULT_ALLOWED_DOMAINS) is None


def test_validated_addresses_are_returned_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    def duplicated(*_args: object, **_kwargs: object):
        entry = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        return [entry, entry]

    monkeypatch.setattr(socket, "getaddrinfo", duplicated)
    assert resolve_allowed_addresses("www.leboncoin.fr", 443, DEFAULT_ALLOWED_DOMAINS) == [
        "93.184.216.34"
    ]


def test_connect_uses_the_validated_numeric_address(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import connect_to_validated

    seen: list[object] = []

    def fake_connection(address, **_kwargs: object):
        seen.append(address)
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_connection)
    connect_to_validated("93.184.216.34", 443)
    assert seen == [("93.184.216.34", 443)], "aucune résolution du nom après validation"


def test_no_fallback_to_an_unvalidated_address(monkeypatch: pytest.MonkeyPatch):
    """Un refus de la seule adresse validée ne doit jamais retomber sur une autre."""
    server_end, client_end = _pair()
    resolutions: list[object] = []
    connections: list[object] = []

    def resolve_once(*args: object, **_kwargs: object):
        resolutions.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def refuse(*args: object, **_kwargs: object):
        connections.append(args)
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "getaddrinfo", resolve_once)
    monkeypatch.setattr(socket, "create_connection", refuse)

    server = Socks5Server()
    thread = threading.Thread(target=server._handle_client, args=(server_end,), daemon=True)
    thread.start()
    try:
        with client_end:
            client_end.sendall(b"\x05\x01\x00")
            assert client_end.recv(2) == b"\x05\x00"
            client_end.sendall(_request_domain("www.leboncoin.fr", 443))
            assert client_end.recv(10)[1] == 5
    finally:
        thread.join(timeout=2.0)
        server.close()

    assert len(resolutions) == 1, "une seule résolution"
    assert connections == [(("93.184.216.34", 443),)], "aucune tentative hors adresse validée"


def test_a_refused_destination_sends_a_failure_reply(monkeypatch: pytest.MonkeyPatch):
    server_end, client_end = _pair()
    monkeypatch.setattr(socket, "getaddrinfo", PRIVATE_ANSWER)

    server = Socks5Server()
    thread = threading.Thread(target=server._handle_client, args=(server_end,), daemon=True)
    thread.start()
    try:
        with client_end:
            client_end.sendall(b"\x05\x01\x00")
            assert client_end.recv(2) == b"\x05\x00"
            client_end.sendall(_request_domain("www.leboncoin.fr", 443))
            assert client_end.recv(10)[1] == 2
    finally:
        thread.join(timeout=2.0)
        server.close()


def test_a_non_loopback_estimate_of_host_is_refused_at_startup():
    with pytest.raises(ValueError, match="non locale"):
        Socks5Server(host="0.0.0.0")


def test_the_explicit_option_accepts_a_non_loopback_bind():
    server = Socks5Server(host="0.0.0.0", allow_non_loopback=True)
    assert server.host == "0.0.0.0"


def test_refusals_are_logged_at_info_without_internal_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    from scripts.leboncoin_residential_relay import (
        DEFAULT_ALLOWED_DOMAINS,
        resolve_allowed_addresses,
    )

    monkeypatch.setattr(socket, "getaddrinfo", PRIVATE_ANSWER)
    with caplog.at_level("INFO", logger="radar.leboncoin_relay"):
        resolve_allowed_addresses("www.leboncoin.fr", 443, DEFAULT_ALLOWED_DOMAINS)

    assert caplog.records, "un refus doit être journalisé"
    assert all(record.levelname in {"INFO", "WARNING", "ERROR"} for record in caplog.records)
    assert "10.1.2.3" not in caplog.text, "aucune adresse interne dans les journaux"


def test_the_connection_cap_refuses_beyond_the_limit():
    server = Socks5Server(max_connections=1)
    assert server._slots.acquire(blocking=False) is True
    try:
        refused = not server._slots.acquire(blocking=False)
    finally:
        server._slots.release()
    assert refused, "le plafond de connexions simultanées est appliqué"


def test_an_invalid_cap_is_refused():
    with pytest.raises(ValueError, match="plafond"):
        Socks5Server(max_connections=0)
    with pytest.raises(ValueError, match="durée"):
        Socks5Server(max_connection_seconds=0)


def test_the_relay_stops_at_the_total_duration(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import _relay

    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    clock = {"now": 1000.0}

    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def advancing_select(_peers, _w, _x, timeout):
        clock["now"] += timeout or 0.0
        return [], [], []

    monkeypatch.setattr("select.select", advancing_select)
    try:
        with client_side, upstream_side, relay_client, relay_upstream:
            _relay(relay_client, relay_upstream, deadline=1000.5, idle_timeout=300.0)
    finally:
        pass
    assert clock["now"] >= 1000.5, "la durée totale borne l'attente"
