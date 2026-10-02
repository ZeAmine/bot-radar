"""Validation sécurisée d'URLs et protection anti-SSRF."""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger("radar.security.url_validator")

FORBIDDEN_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",
    "169.254.169.254",
}


def is_safe_external_url(
    url: str,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    allowed_domains: tuple[str, ...] | None = None,
    check_dns: bool = False,
) -> tuple[bool, str | None]:
    """Valide qu'une URL externe est sécurisée et ne cible pas le réseau privé / SSRF.

    Args:
        url: L'URL à tester.
        allowed_schemes: Schémas autorisés (défaut: http, https).
        allowed_domains: Tuple optionnel de suffixes de domaines autorisés
            (ex: ('vinted.fr', 'vinted.com')).
        check_dns: Si True, résout le nom de domaine pour vérifier qu'il ne
            pointe pas vers une IP privée.

    Returns:
        tuple (is_safe: bool, reason: str | None)
    """
    if not url or not isinstance(url, str):
        return False, "URL vide ou non chaîne"

    try:
        parsed = urlparse(url.strip())
    except Exception as exc:
        return False, f"URL malformée: {exc}"

    if not parsed.scheme or parsed.scheme.lower() not in allowed_schemes:
        return False, f"Schéma non autorisé: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return False, "Nom d'hôte absent"

    hostname_lower = hostname.lower()

    if (
        hostname_lower in FORBIDDEN_HOSTS
        or hostname_lower.endswith(".local")
        or hostname_lower.endswith(".internal")
    ):
        return False, f"Hôte interdit: {hostname}"

    try:
        ip = ipaddress.ip_address(hostname_lower)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False, f"Adresse IP privée ou réservée interdite: {hostname}"
    except ValueError:
        pass

    if allowed_domains and not any(
        hostname_lower == d.lower() or hostname_lower.endswith("." + d.lower())
        for d in allowed_domains
    ):
        return False, f"Domaine non autorisé: {hostname}"

    if check_dns:
        try:
            resolved_ips = socket.getaddrinfo(hostname, None)
            for item in resolved_ips:
                sockaddr = item[4]
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                ):
                    return False, (
                        f"Le domaine {hostname} résout vers une IP privée "
                        f"interdite ({ip_str})"
                    )
        except (OSError, ValueError) as dns_exc:
            return False, f"Échec de résolution DNS pour {hostname}: {dns_exc}"

    return True, None
