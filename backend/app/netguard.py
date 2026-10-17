"""Fetching a URL someone typed, without letting it reach anything private.

A website added on the Sources panel is fetched by this server, so the link
is untrusted:

  http(s) only, ports 80/443, no user:password@
  every address the host resolves to must be on the public internet -- no
    localhost, private network, link-local (incl. the 169.254.169.254 cloud
    metadata endpoint), multicast or reserved ranges
  the connection is made to exactly the address that was checked, so a host
    that answers public then 127.0.0.1 ("DNS rebinding") is still refused
  redirects are followed by hand and every hop is checked again
  3 MB cap; web pages and feeds only
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from .config import settings

MAX_URL_CHARS = 2000
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5

_ALLOWED_PORTS = {80, 443}
_ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml",
                  "text/xml", "application/rss+xml", "application/atom+xml",
                  "application/rdf+xml", "application/feed+json")


class UnsafeUrl(ValueError):
    """A link that cannot or may not be fetched, worded for the reader."""


@dataclass(frozen=True)
class Fetched:
    url: str                # after redirects
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def check_url(url: str) -> str:
    """Validate an untrusted URL. Returns it normalised, or raises UnsafeUrl."""
    url = (url or "").strip()
    if not url:
        raise UnsafeUrl("Paste a link.")
    if len(url) > MAX_URL_CHARS:
        raise UnsafeUrl("That link is too long.")
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Only http and https links can be read.")
    if parts.username or parts.password:
        raise UnsafeUrl("Links with a username or password are not allowed.")
    if not parts.hostname or "." not in parts.hostname.strip("."):
        raise UnsafeUrl("That does not look like a web address.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UnsafeUrl("That link has an invalid port.") from None
    if port not in _ALLOWED_PORTS:
        raise UnsafeUrl("Only standard web ports (80 and 443) are allowed.")
    public_addresses(parts.hostname, port)
    return url


def public_addresses(host: str, port: int) -> list[str]:
    """Resolve `host`, refusing it unless every address is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UnsafeUrl(f"Could not find the site {host}.") from None
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global or address.is_multicast:
            raise UnsafeUrl("That address is on a private or local network and cannot be read.")
        addresses.append(str(address))
    if not addresses:
        raise UnsafeUrl(f"Could not find the site {host}.")
    return addresses


class _PublicOnlyBackend(httpcore.SyncBackend):
    """Connects only to an address checked at the moment of connecting.
    TLS still verifies against the host name, which httpcore passes separately."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = public_addresses(host, port)[0]
        return super().connect_tcp(address, port, timeout=timeout,
                                   local_address=local_address, socket_options=socket_options)


def _guarded_transport() -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport()
    # httpx has no public hook for the network backend, so the pool it built is
    # replaced by one using ours; everything else is httpx's default.
    transport._pool = httpcore.ConnectionPool(ssl_context=httpx.create_ssl_context(),
                                              network_backend=_PublicOnlyBackend())
    return transport


def fetch(url: str) -> Fetched:
    """Fetch an untrusted URL safely. Raises UnsafeUrl with a readable reason."""
    current = check_url(url)
    try:
        with httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent,
                     "Accept": "text/html,application/xhtml+xml,application/rss+xml,"
                               "application/atom+xml,application/xml;q=0.9,*/*;q=0.5"},
            follow_redirects=False,
            transport=_guarded_transport(),
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise UnsafeUrl("The site sent a redirect with no target.")
                        current = check_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise UnsafeUrl(f"The site answered HTTP {resp.status_code}.")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(_ALLOWED_TYPES):
                        raise UnsafeUrl(f"That link is a {ctype} file, not a web page or feed.")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise UnsafeUrl("That page is larger than 3 MB.")
                    return Fetched(url=current, content_type=ctype, body=bytes(body))
    except httpx.TimeoutException:
        raise UnsafeUrl("The site took too long to answer.") from None
    except httpx.HTTPError as exc:
        raise UnsafeUrl(f"Could not reach the site ({type(exc).__name__}).") from None
    raise UnsafeUrl("The site redirected too many times.")
