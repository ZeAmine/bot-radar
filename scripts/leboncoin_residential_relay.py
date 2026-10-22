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
import ipaddress
import logging
import math
import select
import socket
import struct
import threading
import time

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
REPLY_NOT_ALLOWED = 2

DEFAULT_ALLOWED_DOMAINS = ("leboncoin.fr",)
DEFAULT_MAX_CONNECTIONS = 8
DEFAULT_MAX_CONNECTION_SECONDS = 120.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

Candidate = tuple[
    int,
    int,
    int,
    tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
]

NON_PUBLIC_NETWORKS = [
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
        "2002::/16",
        "2001::/32",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
    )
]


class Socks5Server:
    """Serveur SOCKS5 sans authentification, lié à la boucle locale par défaut."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_connection_seconds: float = DEFAULT_MAX_CONNECTION_SECONDS,
        allow_non_loopback: bool = False,
    ) -> None:
        if not allow_non_loopback and host not in LOOPBACK_HOSTS:
            raise ValueError(
                "écoute non locale refusée : le tunnel SSH est la frontière de sécurité"
            )
        self.host = host
        self.port = port
        if not allowed_domains:
            raise ValueError("liste de domaines autorisés vide")
        self.allowed_domains = tuple(normalize_domain(domain) for domain in allowed_domains)
        if isinstance(max_connections, bool) or not isinstance(max_connections, int):
            raise ValueError("plafond de connexions invalide")
        if max_connections < 1:
            raise ValueError("plafond de connexions invalide")
        if (
            isinstance(max_connection_seconds, bool)
            or not isinstance(max_connection_seconds, (int, float))
            or not math.isfinite(max_connection_seconds)
            or max_connection_seconds <= 0
        ):
            raise ValueError("durée maximale de connexion invalide")
        self.max_connections = max_connections
        self.max_connection_seconds = max_connection_seconds
        self._slots = threading.BoundedSemaphore(max_connections)
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
            if not self._slots.acquire(blocking=False):
                logger.info(
                    "Refus SOCKS5 : plafond atteint, fermeture TCP immédiate sans réponse SOCKS5."
                )
                with contextlib.suppress(OSError):
                    client.close()
                continue
            threading.Thread(
                target=self._serve_client, args=(client,), name="socks5-client", daemon=True
            ).start()

    def _serve_client(self, client: socket.socket, holds_slot: bool = True) -> None:
        try:
            self._handle_client(client)
        finally:
            if holds_slot:
                self._slots.release()

    def _handle_client(self, client: socket.socket) -> None:
        deadline = time.monotonic() + self.max_connection_seconds
        upstream: socket.socket | None = None
        watchdog = DeadlineWatchdog(deadline, client)
        watchdog.start()
        try:
            client.settimeout(_remaining(deadline))
            if not _negotiate(client, deadline):
                return
            target = _read_request(client, deadline)
            if target is None:
                return
            host, port = target
            addresses = resolve_allowed_addresses(host, port, self.allowed_domains)
            if addresses is None:
                _send_reply(client, REPLY_NOT_ALLOWED, deadline)
                return
            watchdog.watch(client)
            upstream, reply = _connect_first(addresses, deadline, watchdog)
            if upstream is None:
                _send_reply(client, reply, deadline)
                return
            watchdog.watch(client, upstream)
            _send_reply(client, REPLY_SUCCEEDED, deadline)
            client.settimeout(_remaining(deadline))
            upstream.settimeout(_remaining(deadline))
            _relay(
                client,
                upstream,
                deadline=deadline,
                idle_timeout=RELAY_IDLE_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError):
            logger.debug("Connexion SOCKS5 interrompue.", exc_info=True)
        finally:
            watchdog.stop()
            with contextlib.suppress(OSError):
                client.close()
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("échéance de connexion atteinte")
    return remaining


class DeadlineWatchdog:
    """Force la fermeture des sockets à l'échéance, y compris pendant un blocage système."""

    def __init__(self, deadline: float, *sockets: socket.socket) -> None:
        self._sockets: list[socket.socket] = list(sockets)
        self._deadline = deadline
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="socks5-deadline", daemon=True
        )
        self._thread.start()

    def watch(self, *sockets: socket.socket) -> None:
        with self._lock:
            for sock in sockets:
                if sock not in self._sockets:
                    self._sockets.append(sock)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(max(0.0, self._deadline - time.monotonic())):
            return
        with self._lock:
            sockets = tuple(self._sockets)
        logger.info("Fermeture SOCKS5 : durée maximale de connexion atteinte.")
        for sock in sockets:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                sock.close()


def _connect_first(
    candidates: list[Candidate],
    deadline: float,
    watchdog: DeadlineWatchdog,
) -> tuple[socket.socket | None, int]:
    """Tente les seuls candidats validés, dans le budget restant ; ferme les échecs."""
    refused = False
    for family, socktype, proto, sockaddr in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        candidate: socket.socket | None = None
        try:
            candidate = socket.socket(family, socktype, proto)
            candidate.settimeout(min(CONNECT_TIMEOUT_SECONDS, remaining))
            watchdog.watch(candidate)
            candidate.connect(sockaddr)
            return candidate, REPLY_SUCCEEDED
        except ConnectionRefusedError:
            refused = True
        except OSError:
            pass
        if candidate is not None:
            with contextlib.suppress(OSError):
                candidate.close()
    return None, REPLY_CONNECTION_REFUSED if refused else REPLY_HOST_UNREACHABLE


def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = b""
    while len(data) < size:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("flux SOCKS5 clos prématurément")
        data += chunk
    return data


def _negotiate(client: socket.socket, deadline: float) -> bool:
    """Poignée de main : aucune méthode d'authentification acceptée."""
    version, method_count = struct.unpack("!BB", _recv_exact(client, 2, deadline))
    if version != SOCKS_VERSION:
        return False
    methods = _recv_exact(client, method_count, deadline)
    if METHOD_NO_AUTH not in methods:
        client.sendall(bytes([SOCKS_VERSION, METHOD_UNACCEPTABLE]))
        return False
    client.settimeout(_remaining(deadline))
    client.sendall(bytes([SOCKS_VERSION, METHOD_NO_AUTH]))
    return True


def _read_request(client: socket.socket, deadline: float) -> tuple[str, int] | None:
    """Lit la demande CONNECT et rend (hôte, port), ou None si elle est refusée."""
    header = _recv_exact(client, 4, deadline)
    version, command, _reserved, address_type = struct.unpack("!BBBB", header)
    if version != SOCKS_VERSION or command != COMMAND_CONNECT:
        _send_reply(client, REPLY_GENERAL_FAILURE, deadline)
        return None
    if address_type == ATYP_IPV4:
        host = socket.inet_ntoa(_recv_exact(client, 4, deadline))
    elif address_type == ATYP_IPV6:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16, deadline))
    elif address_type == ATYP_DOMAIN:
        length = _recv_exact(client, 1, deadline)[0]
        host = _recv_exact(client, length, deadline).decode("idna")
    else:
        _send_reply(client, REPLY_GENERAL_FAILURE, deadline)
        return None
    port = struct.unpack("!H", _recv_exact(client, 2, deadline))[0]
    return host, port


def _is_public_address(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        return False
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        return False
    if any(parsed in network for network in NON_PUBLIC_NETWORKS):
        return False
    return parsed.is_global


def normalize_domain(domain: str) -> str:
    """Normalise un domaine autorisé et refuse toute forme ambiguë."""
    candidate = domain.strip().lower()
    if not candidate:
        raise ValueError("domaine autorisé vide")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError(
            f"domaine autorisé invalide (adresse IP littérale) : {candidate!r}"
        )
    candidate = candidate.rstrip(".")
    if not candidate:
        raise ValueError("domaine autorisé vide après normalisation")
    if any(
        character in candidate
        for character in ("*", "?", "/", "\\", ":", "@", "#", "%")
    ) or any(character.isspace() for character in candidate):
        raise ValueError(
            "domaine autorisé invalide (joker, port, chemin ou caractère "
            f"interdit) : {candidate!r}"
        )
    if "." not in candidate:
        raise ValueError(
            f"domaine autorisé invalide (au moins un point attendu) : {candidate!r}"
        )
    if candidate.startswith(".") or ".." in candidate:
        raise ValueError(f"domaine autorisé invalide (étiquette vide) : {candidate!r}")
    for label in candidate.split("."):
        if not all(character.isalnum() or character == "-" for character in label):
            raise ValueError(
                f"domaine autorisé invalide (étiquette {label!r}) : {candidate!r}"
            )
        if label.startswith("-") or label.endswith("-"):
            raise ValueError(
                "domaine autorisé invalide (étiquette finissant par un tiret) : "
                f"{candidate!r}"
            )
    return candidate


def domain_is_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """Le nom doit être le domaine autorisé lui-même ou l'un de ses sous-domaines."""
    normalized = host.strip().rstrip(".").lower()
    if not normalized:
        return False
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in allowed_domains
    )


def resolve_allowed_addresses(
    host: str,
    port: int,
    allowed_domains: tuple[str, ...],
) -> list[Candidate] | None:
    """Résout une seule fois et rend toutes les adresses, ou None si un refus s'impose."""
    try:
        if ipaddress.ip_address(host):
            logger.info("Refus SOCKS5 : destination littérale en adresse IP.")
            return None
    except ValueError:
        pass
    if not domain_is_allowed(host, allowed_domains):
        logger.info("Refus SOCKS5 : domaine hors liste autorisée.")
        return None
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError:
        logger.info("Refus SOCKS5 : résolution impossible.")
        return None
    candidates: list[Candidate] = []
    for family, socktype, proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if not _is_public_address(address):
            logger.info("Refus SOCKS5 : résolution vers une adresse non publique.")
            return None
        candidate: Candidate = (family, socktype, proto, sockaddr)
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        logger.info("Refus SOCKS5 : résolution vide.")
        return None
    return candidates


def _send_reply(client: socket.socket, reply: int, deadline: float) -> None:
    header = struct.pack("!BBBB", SOCKS_VERSION, reply, 0, ATYP_IPV4)
    client.settimeout(_remaining(deadline))
    client.sendall(header + b"\x00\x00\x00\x00\x00\x00")


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    deadline: float | None = None,
    idle_timeout: float = RELAY_IDLE_TIMEOUT_SECONDS,
) -> None:
    """Recopie bidirectionnelle jusqu'à fermeture, inactivité ou échéance de durée."""
    peers = [client, upstream]
    try:
        while True:
            wait = idle_timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.info("Fermeture SOCKS5 : durée maximale de connexion atteinte.")
                    return
                wait = min(wait, remaining)
            readable, _, _ = select.select(peers, [], [], wait)
            if not readable:
                if deadline is not None and time.monotonic() >= deadline:
                    logger.info("Fermeture SOCKS5 : durée maximale de connexion atteinte.")
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
                    if deadline is not None:
                        destination.settimeout(_remaining(deadline))
                    destination.sendall(data)
                except OSError:
                    return
    except OSError:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Relais SOCKS5 résidentiel (Leboncoin).")
    parser.add_argument("--host", default="127.0.0.1", help="adresse d'écoute (boucle locale)")
    parser.add_argument("--port", type=int, default=8080, help="port d'écoute")
    parser.add_argument(
        "--allowed-domain",
        action="append",
        metavar="DOMAINE",
        help="domaine autorisé, répétable (défaut : leboncoin.fr et ses sous-domaines)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help="connexions simultanées autorisées",
    )
    parser.add_argument(
        "--max-connection-seconds",
        type=float,
        default=DEFAULT_MAX_CONNECTION_SECONDS,
        help="durée totale maximale d'une connexion",
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="écoute hors boucle locale : retire la protection portée par le tunnel SSH",
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    allowed_domains = (
        tuple(arguments.allowed_domain)
        if arguments.allowed_domain
        else DEFAULT_ALLOWED_DOMAINS
    )
    server = Socks5Server(
        host=arguments.host,
        port=arguments.port,
        allowed_domains=allowed_domains,
        max_connections=arguments.max_connections,
        max_connection_seconds=arguments.max_connection_seconds,
        allow_non_loopback=arguments.allow_non_loopback,
    )
    port = server.start()
    logger.info(
        "Relais SOCKS5 écoute sur %s:%d (domaines autorisés : %s)",
        arguments.host,
        port,
        ", ".join(sorted(allowed_domains)),
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("Arrêt du relais SOCKS5.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
