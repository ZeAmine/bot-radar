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
            client_end.sendall(_request_ipv4("127.0.0.1", 80))
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
            client_end.sendall(_request_ipv4("127.0.0.1", 80))
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
