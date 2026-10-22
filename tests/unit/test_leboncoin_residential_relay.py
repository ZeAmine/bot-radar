"""Tests du relais SOCKS5 résidentiel.

La recette isolée refuse toute connexion réseau réelle, y compris la boucle
locale : le protocole est donc exercé sur des paires de sockets, la résolution
et la création de sockets sortantes sont doublées. Aucun port n'est ouvert,
aucun paquet ne sort.
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

FUTURE = time.monotonic() + 600.0


class _UpstreamProxy:
    """Socket sortante doublée : délègue les E/S à une paire de sockets locale."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.connected: object = None
        self.closed = False

    def connect(self, sockaddr: object) -> None:
        self.connected = sockaddr

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def recv(self, size: int) -> bytes:
        return self._sock.recv(size)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def fileno(self) -> int:
        return self._sock.fileno()

    def shutdown(self, _how: int) -> None:
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.closed = True
        with contextlib.suppress(OSError):
            self._sock.close()


def _request_domain(host: str, port: int) -> bytes:
    encoded = host.encode("idna")
    header = struct.pack("!BBBB", 5, 1, 0, 3) + bytes([len(encoded)]) + encoded
    return header + struct.pack("!H", port)


def _fake_resolution(address: str):
    def infos(*_args: object, **_kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    return infos


def _fake_socket(created: list[object]):
    class FakeSocket:
        def __init__(self, family: int, socktype: int, proto: int, *_args: object) -> None:
            self.family = family
            self.socktype = socktype
            self.proto = proto
            self.connected: object = None
            self.closed = False

        def settimeout(self, _value: float) -> None:
            pass

        def connect(self, sockaddr: object) -> None:
            self.connected = sockaddr

        def close(self) -> None:
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    def factory(family: int, socktype: int, proto: int, *_args: object) -> FakeSocket:
        if family in (socket.AF_INET, socket.AF_INET6):
            created.append((family, socktype, proto))
        return FakeSocket(family, socktype, proto)

    return factory


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def _request_ipv4(host: str, port: int) -> bytes:
    return struct.pack("!BBBB", 5, 1, 0, 1) + socket.inet_aton(host) + struct.pack("!H", port)


def test_negotiate_accepts_no_auth():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x05\x01\x00")
        assert _negotiate(server, FUTURE) is True
        assert client.recv(2) == b"\x05\x00"


def test_negotiate_refuses_missing_method():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x05\x01\x02")
        assert _negotiate(server, FUTURE) is False
        assert client.recv(2) == b"\x05\xff"


def test_negotiate_refuses_foreign_version():
    server, client = _pair()
    with server, client:
        client.sendall(b"\x04\x01\x00")
        assert _negotiate(server, FUTURE) is False


def test_read_request_ipv4_and_domain():
    server, client = _pair()
    with server, client:
        client.sendall(_request_ipv4("192.0.2.10", 8080))
        assert _read_request(server, FUTURE) == ("192.0.2.10", 8080)

    server, client = _pair()
    with server, client:
        payload = struct.pack("!BBBB", 5, 1, 0, 3) + b"\x0bexample.com" + struct.pack("!H", 443)
        client.sendall(payload)
        assert _read_request(server, FUTURE) == ("example.com", 443)


def test_read_request_refuses_unknown_command():
    server, client = _pair()
    with server, client:
        client.sendall(struct.pack("!BBBB", 5, 2, 0, 1) + b"\x00" * 6)
        assert _read_request(server, FUTURE) is None
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
    proxy = _UpstreamProxy(upstream_local)
    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: proxy)

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
            assert proxy.connected == ("93.184.216.34", 443)
            client_end.sendall(b"blacksad")
            assert upstream_remote.recv(8) == b"blacksad"
            upstream_remote.sendall(b"\x00ki")
            assert client_end.recv(3) == b"\x00ki"
    finally:
        thread.join(timeout=2.0)
        server.close()


def test_handle_client_reports_refused_connection(monkeypatch: pytest.MonkeyPatch):
    server_end, client_end = _pair()

    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution("93.184.216.34"))

    def refusing_socket(*_args: object, **_kwargs: object):
        sock = _FakeSocket(socket.AF_INET, socket.SOCK_STREAM, 6)

        def refuse_connect(_sockaddr: object) -> None:
            raise ConnectionRefusedError

        sock.connect = refuse_connect
        return sock

    monkeypatch.setattr(socket, "socket", refusing_socket)

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
    assert resolve_allowed_addresses("api.ipify.org", 443, allowed) == [
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443))
    ]


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
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443))
    ]


def test_connect_uses_the_validated_sockaddr_without_create_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _connect_first

    created: list[object] = []
    monkeypatch.setattr(socket, "socket", _fake_socket(created))

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("create_connection ne doit plus être utilisé")

    monkeypatch.setattr(socket, "create_connection", forbidden)

    client_side, relay_client = _pair()
    watchdog = DeadlineWatchdog(FUTURE, relay_client)
    try:
        with client_side, relay_client:
            upstream, reply = _connect_first(
                [(socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443))],
                FUTURE,
                watchdog,
            )
    finally:
        watchdog.stop()
    assert reply == 0
    assert upstream is not None
    assert upstream.connected == ("93.184.216.34", 443)
    assert created == [(socket.AF_INET, socket.SOCK_STREAM, 6)]


def test_an_ipv6_candidate_is_connected_with_its_own_family(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _connect_first

    created: list[object] = []
    monkeypatch.setattr(socket, "socket", _fake_socket(created))

    client_side, relay_client = _pair()
    watchdog = DeadlineWatchdog(FUTURE, relay_client)
    sockaddr = ("2a00:1450:4007:810::200e", 443, 0, 0)
    try:
        with client_side, relay_client:
            upstream, reply = _connect_first(
                [(socket.AF_INET6, socket.SOCK_STREAM, 6, sockaddr)], FUTURE, watchdog
            )
    finally:
        watchdog.stop()
    assert reply == 0
    assert upstream is not None
    assert upstream.family == socket.AF_INET6
    assert upstream.connected == sockaddr


def test_a_failed_candidate_is_closed_and_the_next_validated_one_is_used(
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _connect_first

    made: list[_FakeSocket] = []
    client_side, relay_client = _pair()

    class Factory:
        def __call__(
            self, family: int, socktype: int, proto: int, *_args: object
        ) -> _FakeSocket:
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("seules les sockets sortantes doivent être créées")
            sock = _FakeSocket(family, socktype, proto)
            if not made:

                def refuse(_sockaddr: object) -> None:
                    raise ConnectionRefusedError

                sock.connect = refuse
            made.append(sock)
            return sock

    monkeypatch.setattr(socket, "socket", Factory())

    watchdog = DeadlineWatchdog(FUTURE, relay_client)
    candidates = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("1.1.1.1", 443)),
    ]
    try:
        with client_side, relay_client:
            upstream, reply = _connect_first(candidates, FUTURE, watchdog)
    finally:
        monkeypatch.undo()
        watchdog.stop()
    assert reply == 0
    assert upstream is not None
    assert upstream.connected == ("1.1.1.1", 443)
    assert len(made) == 2, "deux candidats, deux sockets"
    assert made[0].closed, "la socket du candidat échoué est fermée"
    assert not made[1].closed, "la socket retenue reste ouverte"


def test_every_failed_candidate_socket_is_closed(monkeypatch: pytest.MonkeyPatch):
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _connect_first

    made: list[_FakeSocket] = []

    def factory(family: int, socktype: int, proto: int, *_args: object):
        if family not in (socket.AF_INET, socket.AF_INET6):
            return _FakeSocket(family, socktype, proto)
        sock = _FakeSocket(family, socktype, proto)

        def refuse(_sockaddr: object) -> None:
            raise ConnectionRefusedError

        sock.connect = refuse
        made.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", factory)

    client_side, relay_client = _pair()
    watchdog = DeadlineWatchdog(FUTURE, relay_client)
    candidates = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("1.1.1.1", 443)),
    ]
    try:
        with client_side, relay_client:
            upstream, reply = _connect_first(candidates, FUTURE, watchdog)
    finally:
        monkeypatch.undo()
        watchdog.stop()
    assert upstream is None
    assert reply == 5
    assert len(made) == 2, "une socket par candidat, aucune en plus"
    assert all(sock.closed for sock in made), "chaque échec ferme sa socket"


class _FakeSocket:
    def __init__(self, family: int, socktype: int, proto: int) -> None:
        self.family = family
        self.socktype = socktype
        self.proto = proto
        self.connected: object = None
        self.closed = False

    def settimeout(self, _value: float) -> None:
        pass

    def connect(self, sockaddr: object) -> None:
        self.connected = sockaddr

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _factory(created: list[object], failing_first: bool):
    made: list[_FakeSocket] = []

    def factory(family: int, socktype: int, proto: int, *_args: object) -> _FakeSocket:
        if family in (socket.AF_INET, socket.AF_INET6):
            created.append((family, socktype, proto))
        sock = _FakeSocket(family, socktype, proto)
        if failing_first and not made:

            def refuse(_sockaddr: object) -> None:
                raise ConnectionRefusedError

            sock.connect = refuse
        made.append(sock)
        return sock

    return factory


def test_no_fallback_to_an_unvalidated_address(monkeypatch: pytest.MonkeyPatch):
    """Un refus de la seule adresse validée ne doit jamais retomber sur une autre."""
    server_end, client_end = _pair()
    resolutions: list[object] = []
    connections: list[object] = []

    def resolve_once(*args: object, **_kwargs: object):
        resolutions.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def make_socket(family: int, socktype: int, proto: int, *_args: object):
        sock = _FakeSocket(family, socktype, proto)

        def refuse(sockaddr: object) -> None:
            connections.append(sockaddr)
            raise ConnectionRefusedError

        sock.connect = refuse
        return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve_once)
    monkeypatch.setattr(socket, "socket", make_socket)

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
    assert connections == [("93.184.216.34", 443)], "aucune tentative hors adresse validée"


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


class _FakeListener:
    """Écoute doublée : rend les clients préparés, puis bloque sur un Event."""

    def __init__(self, clients: list[socket.socket], released: threading.Event) -> None:
        self._clients = list(clients)
        self._released = released
        self.closed = False

    def accept(self) -> tuple[socket.socket, tuple[()]]:
        while self._clients:
            return self._clients.pop(0), ()
        self._released.wait(5.0)
        raise OSError("écoute fermée")

    def close(self) -> None:
        self.closed = True

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 1)


def test_the_cap_admits_up_to_the_limit_and_refuses_the_next(
    monkeypatch: pytest.MonkeyPatch,
):
    """Le chemin d'acceptation réel applique le plafond, sans worker en trop."""
    released = threading.Event()
    admitted = [_pair() for _ in range(2)]
    extra = _pair()
    listener = _FakeListener([pair[0] for pair in admitted] + [extra[0]], released)
    monkeypatch.setattr(socket, "create_server", lambda *_a, **_k: listener)

    server = Socks5Server(port=0, max_connections=2)
    handlers: list[tuple[socket.socket, threading.Event]] = []
    real_thread = threading.Thread
    gate = threading.Event()

    def blocking_handler(_client: socket.socket) -> None:
        gate.wait(5.0)

    monkeypatch.setattr(server, "_handle_client", blocking_handler)
    watching: list[threading.Thread] = []

    def tracking_thread(*args: object, **kwargs: object) -> threading.Thread:
        thread = real_thread(*args, **kwargs)
        if kwargs.get("name") == "socks5-client":
            watching.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", tracking_thread)
    server._socket = listener
    accept = real_thread(target=server._accept_loop, daemon=True)
    accept.start()
    try:
        give_time(0.2)
        assert len(watching) == 2, "deux workers admis, pas trois"
        assert extra[1].recv(1) == b"", "le client en trop est fermé sans réponse SOCKS5"
    finally:
        gate.set()
        released.set()
        accept.join(timeout=5.0)
        for client, probe in admitted + [extra]:
            with contextlib.suppress(OSError):
                client.close()
            with contextlib.suppress(OSError):
                probe.close()
    assert handlers == []


def give_time(seconds: float) -> None:
    time.sleep(seconds)


def test_a_slot_is_released_after_the_worker_ends():
    server = Socks5Server(max_connections=1)
    assert server._slots.acquire(blocking=False) is True

    class Exploding:
        def settimeout(self, _value: float) -> None:
            raise OSError("panne simulée")

        def close(self) -> None:
            pass

    server._serve_client(Exploding())  # type: ignore[arg-type]
    assert server._slots.acquire(blocking=False) is True, "la place est rendue après exception"
    server._slots.release()


def test_a_slot_is_released_after_a_normal_end():
    """Fin normale : poignée de main refusée par le client, slot rendu."""
    server = Socks5Server(max_connections=1, max_connection_seconds=5.0)
    server_end, client_end = _pair()
    assert server._slots.acquire(blocking=False) is True
    done = threading.Event()

    def run() -> None:
        server._serve_client(server_end)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        with client_end:
            client_end.sendall(b"\x04\x01\x00")
            assert done.wait(3.0), "le worker termine sur un protocole étranger"
    finally:
        with contextlib.suppress(OSError):
            client_end.close()
    assert server._slots.acquire(blocking=False) is True


def test_an_invalid_cap_is_refused():
    with pytest.raises(ValueError, match="plafond"):
        Socks5Server(max_connections=0)
    with pytest.raises(ValueError, match="durée"):
        Socks5Server(max_connection_seconds=0)


def test_the_relay_stops_at_the_total_duration():
    """Preuve en temps réel : la durée totale borne un relais inactif."""
    from scripts.leboncoin_residential_relay import _relay

    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    started = time.monotonic()
    thread = threading.Thread(
        target=_relay,
        args=(relay_client, relay_upstream),
        kwargs={"deadline": started + 0.3, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "le relais rend la main à l'échéance"
        assert time.monotonic() - started < 2.0
    finally:
        for sock in (client_side, upstream_side, relay_client, relay_upstream):
            with contextlib.suppress(OSError):
                sock.close()


def test_the_watchdog_closes_sockets_at_the_deadline():
    from scripts.leboncoin_residential_relay import DeadlineWatchdog

    server_end, client_end = _pair()
    watchdog = DeadlineWatchdog(time.monotonic() + 0.2, server_end)
    watchdog.start()
    try:
        client_end.settimeout(3.0)
        assert client_end.recv(1) == b"", "la socket est fermée à l'échéance"
    finally:
        watchdog.stop()
        with contextlib.suppress(OSError):
            client_end.close()


def test_a_slow_client_is_cut_at_the_deadline_and_its_slot_released():
    """Client qui n'envoie jamais sa poignée de main : coupé, slot rendu."""
    server = Socks5Server(max_connections=1, max_connection_seconds=0.3)
    server_end, client_end = _pair()
    assert server._slots.acquire(blocking=False) is True
    done = threading.Event()

    def run() -> None:
        server._serve_client(server_end)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        assert done.wait(3.0), "le worker termine malgré un client muet"
        assert time.monotonic() - started < 2.0
        assert server._slots.acquire(blocking=False) is True, "le slot est libéré"
        server._slots.release()
    finally:
        with contextlib.suppress(OSError):
            client_end.close()
        with contextlib.suppress(OSError):
            server_end.close()


def test_a_client_sending_byte_by_byte_is_cut_at_the_deadline():
    """Client lent : chaque octet arrive avant l'échéance, la durée totale tranche."""
    server = Socks5Server(max_connections=1, max_connection_seconds=0.4)
    server_end, client_end = _pair()
    assert server._slots.acquire(blocking=False) is True
    done = threading.Event()

    def run() -> None:
        server._serve_client(server_end)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        with contextlib.suppress(OSError):
            for byte in b"\x05\x01\x00":
                client_end.sendall(bytes([byte]))
                time.sleep(0.15)
        assert done.wait(3.0), "le worker termine"
        assert time.monotonic() - started < 2.0
        assert server._slots.acquire(blocking=False) is True
        server._slots.release()
    finally:
        with contextlib.suppress(OSError):
            client_end.close()
        with contextlib.suppress(OSError):
            server_end.close()


def test_a_receiving_peer_that_stops_reading_is_cut_at_the_deadline():
    """Écriture qui ne progresse plus : l'échéance ferme, aucune écriture bloquante."""
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _relay

    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    deadline = time.monotonic() + 0.4
    watchdog = DeadlineWatchdog(deadline, relay_client, relay_upstream)
    watchdog.start()

    class StalledUpstream:
        """Sortant doublé : ne lit plus (sendall échoue), E/S déléguées à la paire."""

        def __init__(self, sock: socket.socket) -> None:
            self._sock = sock

        def fileno(self) -> int:
            return self._sock.fileno()

        def recv(self, size: int) -> bytes:
            return self._sock.recv(size)

        def sendall(self, _data: bytes) -> None:
            raise OSError("le pair ne lit plus")

        def settimeout(self, value: float | None) -> None:
            self._sock.settimeout(value)

        def shutdown(self, _how: int) -> None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)

        def close(self) -> None:
            with contextlib.suppress(OSError):
                self._sock.close()

    stalled = StalledUpstream(relay_upstream)
    thread = threading.Thread(
        target=_relay,
        args=(relay_client, stalled),
        kwargs={"deadline": deadline, "idle_timeout": 30.0},
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    try:
        client_side.sendall(b"blocked")
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2.0
    finally:
        watchdog.stop()
        for sock in (client_side, upstream_side, relay_client, relay_upstream):
            with contextlib.suppress(OSError):
                sock.close()


def test_repeated_connect_failures_respect_the_global_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    """Plusieurs échecs de connexion : le budget global borne les tentatives."""
    from scripts.leboncoin_residential_relay import DeadlineWatchdog, _connect_first

    attempts: list[object] = []

    def factory(family: int, socktype: int, proto: int, *_args: object) -> _FakeSocket:
        sock = _FakeSocket(family, socktype, proto)
        original = sock.connect

        def slow_failure(sockaddr: object) -> None:
            attempts.append(sockaddr)
            time.sleep(0.15)
            original(sockaddr)
            raise OSError("échec")

        sock.connect = slow_failure
        return sock

    monkeypatch.setattr(socket, "socket", factory)
    client_side, relay_client = _pair()
    watchdog = DeadlineWatchdog(time.monotonic() + 0.3, relay_client)
    watchdog.start()
    candidates = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, (f"1.1.1.{index}", 443)) for index in range(1, 7)
    ]
    started = time.monotonic()
    try:
        upstream, reply = _connect_first(candidates, started + 0.3, watchdog)
        elapsed = time.monotonic() - started
    finally:
        monkeypatch.undo()
        watchdog.stop()
        with contextlib.suppress(OSError):
            client_side.close()
        with contextlib.suppress(OSError):
            relay_client.close()
    assert upstream is None
    assert reply == 4
    assert elapsed < 2.0, "le budget global borne l'ensemble des tentatives"
    assert len(attempts) < len(candidates), "toutes les tentatives n'ont pas été épuisées"


def test_max_connection_seconds_must_be_finite_and_positive():
    for value in (0, -1.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="durée"):
            Socks5Server(max_connection_seconds=value)


def test_max_connections_must_be_a_positive_integer():
    for value in (0, -2):
        with pytest.raises(ValueError, match="plafond"):
            Socks5Server(max_connections=value)


def test_the_watchdog_cuts_an_operation_that_ignores_socket_timeouts():
    """Backstop : une opération non interruptible par settimeout est coupée à l'échéance."""
    from scripts.leboncoin_residential_relay import DeadlineWatchdog

    class Uninterruptible:
        """Imite un appel système qui ignore les timeouts de socket."""

        def __init__(self) -> None:
            self.server_end, self.client_end = socket.socketpair()
            self.released = threading.Event()

        def settimeout(self, _value: float | None) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            self.released.wait(30.0)
            return b""

        def shutdown(self, _how: int) -> None:
            self.released.set()

        def close(self) -> None:
            self.released.set()

    probe = Uninterruptible()
    watchdog = DeadlineWatchdog(time.monotonic() + 0.3, probe)
    watchdog.start()
    done = threading.Event()

    def run() -> None:
        probe.recv(1)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        assert done.wait(3.0), "le worker est libéré par le watchdog"
        assert time.monotonic() - started < 2.0
    finally:
        watchdog.stop()
        probe.server_end.close()
        probe.client_end.close()


def test_the_watchdog_is_the_backstop_when_socket_timeouts_do_not_apply():
    """Sans watchdog, un recv qui ignore son timeout ne rend jamais la main."""
    server = Socks5Server(max_connections=1, max_connection_seconds=0.3)
    server_end, client_end = _pair()
    assert server._slots.acquire(blocking=False) is True
    released = threading.Event()

    class IgnoresTimeout:
        """Socket client dont les timeouts n'ont aucun effet (appel système opaque)."""

        def __init__(self, sock: socket.socket) -> None:
            self._sock = sock
            self.closed = False

        def settimeout(self, _value: float | None) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            released.wait(30.0)
            return b""

        def sendall(self, _data: bytes) -> None:
            pass

        def shutdown(self, _how: int) -> None:
            released.set()

        def close(self) -> None:
            self.closed = True

    probe = IgnoresTimeout(server_end)
    done = threading.Event()

    def run() -> None:
        server._serve_client(probe)  # type: ignore[arg-type]
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        finished = done.wait(3.0)
        elapsed = time.monotonic() - started
    finally:
        released.set()
        thread.join(timeout=3.0)
        for sock in (client_end, server_end):
            with contextlib.suppress(OSError):
                sock.close()
    assert finished, "le worker rend la main malgré des timeouts ignorés"
    assert elapsed < 2.0, "c'est le watchdog, et non le timeout de socket, qui libère"
    assert elapsed >= 0.2, "la coupure a bien attendu l'échéance"


def test_a_send_slower_than_the_connect_timeout_still_succeeds_within_the_deadline():
    """Le relais ne réutilise pas le timeout de connexion : il reste borné par la deadline."""
    from scripts.leboncoin_residential_relay import _relay

    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    deadline = time.monotonic() + 5.0
    thread = threading.Thread(
        target=_relay,
        args=(relay_client, relay_upstream),
        kwargs={"deadline": deadline, "idle_timeout": 30.0},
        daemon=True,
    )
    thread.start()
    try:
        client_side.settimeout(5.0)
        upstream_side.settimeout(5.0)
        time.sleep(0.6)
        client_side.sendall(b"tardif")
        assert upstream_side.recv(6) == b"tardif", "un envoi après le timeout de connexion passe"
        upstream_side.sendall(b"reponse")
        assert client_side.recv(7) == b"reponse"
    finally:
        for sock in (client_side, upstream_side, relay_client, relay_upstream):
            with contextlib.suppress(OSError):
                sock.close()
        thread.join(timeout=3.0)


def test_a_send_beyond_the_deadline_is_cut():
    """Un pair qui ne lit plus est coupé à l'échéance, même en cours d'écriture."""
    client_side, relay_client = _pair()
    relay_upstream, upstream_side = _pair()
    deadline = time.monotonic() + 0.3
    watchdog = __import__(
        "scripts.leboncoin_residential_relay", fromlist=["DeadlineWatchdog"]
    ).DeadlineWatchdog(deadline, relay_client, relay_upstream)
    watchdog.start()

    class NeverReads:
        def __init__(self, sock: socket.socket) -> None:
            self._sock = sock

        def fileno(self) -> int:
            return self._sock.fileno()

        def recv(self, size: int) -> bytes:
            return self._sock.recv(size)

        def settimeout(self, value: float | None) -> None:
            self._sock.settimeout(value)

        def sendall(self, data: bytes) -> None:
            while True:
                self._sock.sendall(data)

        def shutdown(self, _how: int) -> None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)

        def close(self) -> None:
            with contextlib.suppress(OSError):
                self._sock.close()

    from scripts.leboncoin_residential_relay import _relay

    thread = threading.Thread(
        target=_relay,
        args=(relay_client, NeverReads(relay_upstream)),
        kwargs={"deadline": deadline, "idle_timeout": 30.0},
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    try:
        with contextlib.suppress(OSError):
            client_side.sendall(b"x" * 65536)
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "le relais rend la main à l'échéance"
        assert time.monotonic() - started < 2.0
    finally:
        watchdog.stop()
        for sock in (client_side, upstream_side, relay_client, relay_upstream):
            with contextlib.suppress(OSError):
                sock.close()


@pytest.mark.parametrize(
    ("domain", "reason"),
    [
        ("", "vide"),
        ("   ", "vide"),
        ("leboncoin", "point"),
        ("*", "joker"),
        ("*.leboncoin.fr", "joker"),
        ("leboncoin.fr/*", "joker"),
        ("leboncoin.fr:443", "port"),
        ("leboncoin.fr/annonces", "chemin"),
        ("93.184.216.34", "adresse IP"),
        ("2a00:1450:4007:810::200e", "adresse IP"),
        (".leboncoin.fr", "étiquette vide"),
        ("leboncoin..fr", "étiquette vide"),
        ("-leboncoin.fr", "tiret"),
        ("leboncoin-.fr", "tiret"),
        ("lebon coin.fr", "caractère interdit"),
    ],
)
def test_invalid_allowed_domains_are_refused_at_startup(domain: str, reason: str):
    from scripts.leboncoin_residential_relay import normalize_domain

    with pytest.raises(ValueError, match=reason):
        normalize_domain(domain)


def test_allowed_domains_are_normalized_at_startup():
    from scripts.leboncoin_residential_relay import normalize_domain

    assert normalize_domain("LeBonCoin.FR") == "leboncoin.fr"
    assert normalize_domain("  www.leboncoin.fr.  ") == "www.leboncoin.fr"
    server = Socks5Server(allowed_domains=("Leboncoin.FR.",))
    assert server.allowed_domains == ("leboncoin.fr",)


def test_an_empty_allowed_domain_list_is_refused():
    with pytest.raises(ValueError, match="vide"):
        Socks5Server(allowed_domains=())


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::10.0.0.1", "2002:0a00:0001::1", "2001::1", "64:ff9b:1::1"],
)
def test_ipv6_translation_prefixes_are_not_public(address: str):
    from scripts.leboncoin_residential_relay import _is_public_address

    assert not _is_public_address(address), "préfixe de traduction IPv4 en IPv6 refusé"


def test_a_public_ipv6_address_stays_allowed():
    from scripts.leboncoin_residential_relay import _is_public_address

    assert _is_public_address("2a00:1450:4007:810::200e")
