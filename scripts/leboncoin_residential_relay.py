"""Relais SOCKS5 minimal pour la sortie résidentielle de la collecte Leboncoin.

Destiné au Raspberry Pi du domicile : le VPS ouvre un tunnel SSH inverse vers ce
service (`ssh -R 127.0.0.1:1080:127.0.0.1:8080`), et `curl_cffi` sort par
`socks5://127.0.0.1:1080`. Aucun privilège, aucun port entrant, aucune
dépendance : seule la bibliothèque standard est utilisée.

Ce relais ne fournit ni authentification ni écoute publique ; il doit rester
attaché à la boucle locale, le tunnel SSH étant la frontière de sécurité.
"""

import argparse
import contextlib
import logging
import select
import socket
import struct
import threading

logger = logging.getLogger("radar.leboncoin_relay")

BUFFER_SIZE = 65536
RELAY_IDLE_TIMEOUT_SECONDS = 300.0
CONNECT_TIMEOUT_SECONDS = 15.0

SOCKS_VERSION = 5
METHOD_NO_AUTH = 0
METHOD_UNACCEPTABLE = 0xFF
COMMAND_CONNECT = 1
ATYP_IPV4 = 1
ATYP_DOMAIN = 3
ATYP_IPV6 = 4

REPLY_SUCCEEDED = 0
REPLY_GENERAL_FAILURE = 1
REPLY_HOST_UNREACHABLE = 4
REPLY_CONNECTION_REFUSED = 5


class Socks5Server:
    """Serveur SOCKS5 sans authentification, lié à la boucle locale par défaut."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def start(self) -> int:
        """Ouvre l'écoute et démarre la boucle d'acceptation ; rend le port réel."""
        listener = socket.create_server((self.host, self.port))
        self._socket = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(
            target=self._accept_loop, name="socks5-accept", daemon=True
        )
        self._thread.start()
        return self.port

    def close(self) -> None:
        self._stopped.set()
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        assert self._socket is not None
        while not self._stopped.is_set():
            try:
                client, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_client, args=(client,), name="socks5-client", daemon=True
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(CONNECT_TIMEOUT_SECONDS)
            if not _negotiate(client):
                return
            target = _read_request(client)
            if target is None:
                return
            host, port = target
            try:
                upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
            except ConnectionRefusedError:
                _send_reply(client, REPLY_CONNECTION_REFUSED)
                return
            except OSError:
                _send_reply(client, REPLY_HOST_UNREACHABLE)
                return
            _send_reply(client, REPLY_SUCCEEDED)
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream)
        except (OSError, ValueError):
            logger.debug("Connexion SOCKS5 interrompue.", exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                client.close()
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("flux SOCKS5 clos prématurément")
        data += chunk
    return data


def _negotiate(client: socket.socket) -> bool:
    """Poignée de main : aucune méthode d'authentification acceptée."""
    version, method_count = struct.unpack("!BB", _recv_exact(client, 2))
    if version != SOCKS_VERSION:
        return False
    methods = _recv_exact(client, method_count)
    if METHOD_NO_AUTH not in methods:
        client.sendall(bytes([SOCKS_VERSION, METHOD_UNACCEPTABLE]))
        return False
    client.sendall(bytes([SOCKS_VERSION, METHOD_NO_AUTH]))
    return True


def _read_request(client: socket.socket) -> tuple[str, int] | None:
    """Lit la demande CONNECT et rend (hôte, port), ou None si elle est refusée."""
    version, command, _reserved, address_type = struct.unpack("!BBBB", _recv_exact(client, 4))
    if version != SOCKS_VERSION or command != COMMAND_CONNECT:
        _send_reply(client, REPLY_GENERAL_FAILURE)
        return None
    if address_type == ATYP_IPV4:
        host = socket.inet_ntoa(_recv_exact(client, 4))
    elif address_type == ATYP_IPV6:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
    elif address_type == ATYP_DOMAIN:
        length = _recv_exact(client, 1)[0]
        host = _recv_exact(client, length).decode("idna")
    else:
        _send_reply(client, REPLY_GENERAL_FAILURE)
        return None
    port = struct.unpack("!H", _recv_exact(client, 2))[0]
    return host, port


def _send_reply(client: socket.socket, reply: int) -> None:
    header = struct.pack("!BBBB", SOCKS_VERSION, reply, 0, ATYP_IPV4)
    client.sendall(header + b"\x00\x00\x00\x00\x00\x00")


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Recopie bidirectionnelle jusqu'à fermeture ou inactivité prolongée."""
    peers = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(peers, [], [], RELAY_IDLE_TIMEOUT_SECONDS)
            if not readable:
                return
            for source in readable:
                try:
                    data = source.recv(BUFFER_SIZE)
                except OSError:
                    return
                if not data:
                    return
                destination = upstream if source is client else client
                try:
                    destination.sendall(data)
                except OSError:
                    return
    except OSError:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Relais SOCKS5 résidentiel (Leboncoin).")
    parser.add_argument("--host", default="127.0.0.1", help="adresse d'écoute (boucle locale)")
    parser.add_argument("--port", type=int, default=8080, help="port d'écoute")
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = Socks5Server(host=arguments.host, port=arguments.port)
    port = server.start()
    logger.info("Relais SOCKS5 écoute sur %s:%d", arguments.host, port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("Arrêt du relais SOCKS5.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
