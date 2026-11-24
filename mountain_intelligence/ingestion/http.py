"""Hardened HTTP fetching for ingestion (SSRF-safe, polite, bounded).

Controls:
- Only http/https, default ports, no credentials in URLs, host must be in the source allowlist.
- Every redirect hop is re-validated (redirects are followed manually, never automatically).
- DNS is resolved by our own network backend at *connection time* and every resolved address
  must be globally routable; the socket connects to the vetted address (defeats DNS rebinding
  between check and use). Environment proxies are ignored so the check is meaningful.
- Timeouts, bounded response size, bounded retries with backoff, per-domain rate limiting,
  robots.txt, and conditional requests (ETag / Last-Modified).
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpcore2
import httpx2
from django.conf import settings

from .contracts import DiscoveredItem, FetchedDocument

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    def __init__(self, kind: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind  # blocked | robots | not_found | http_error | too_large | network
        self.status_code = status_code


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class GuardedNetworkBackend(httpcore2.SyncBackend):
    """Resolves and vets the destination right before connecting, then pins the vetted IP."""

    def __init__(self, resolver: Resolver | None = None):
        super().__init__()
        self._resolver = resolver or default_resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore2.NetworkStream:
        addresses = self._resolver(host, port)
        if not addresses:
            raise httpcore2.ConnectError(f"no address for {host}")
        blocked = [a for a in addresses if not is_public_address(a)]
        if blocked:
            raise httpcore2.ConnectError(f"destination {host} resolves to non-public address")
        return super().connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )


class GuardedTransport(httpx2.HTTPTransport):
    def __init__(self, resolver: Resolver | None = None, verify: bool = True):
        super().__init__(verify=verify, trust_env=False)
        self._pool = httpcore2.ConnectionPool(
            ssl_context=httpx2.create_ssl_context(verify=verify, trust_env=False),
            max_connections=10,
            keepalive_expiry=5.0,
            network_backend=GuardedNetworkBackend(resolver),
        )


@dataclass
class UrlPolicy:
    allowed_domains: list[str]
    allowed_ports: frozenset[int] = frozenset({80, 443})

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise FetchError("blocked", f"scheme not allowed: {parts.scheme!r}")
        if parts.username or parts.password:
            raise FetchError("blocked", "credentials in URL are not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise FetchError("blocked", "missing host")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in self.allowed_ports:
            raise FetchError("blocked", f"port not allowed: {port}")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not is_public_address(str(ip)):
            raise FetchError("blocked", "non-public IP literal")
        if not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise FetchError("blocked", f"host {host!r} is not in the source allowlist")


@dataclass
class _DomainThrottle:
    min_delay: float
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last.get(host, 0.0) + self.min_delay - now
            self._last[host] = max(now, now + delay)
        if delay > 0:
            time.sleep(delay)


class SafeFetcher:
    def __init__(
        self,
        allowed_domains: list[str],
        *,
        transport: httpx2.BaseTransport | None = None,
        min_delay: float | None = None,
        max_bytes: int | None = None,
        max_redirects: int | None = None,
        max_retries: int | None = None,
        respect_robots: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.policy = UrlPolicy([d.lower().strip(".") for d in allowed_domains])
        if not self.policy.allowed_domains:
            raise ValueError("a fetcher requires a non-empty domain allowlist")
        self.max_bytes = max_bytes or settings.INGESTION_MAX_RESPONSE_BYTES
        self.max_redirects = settings.INGESTION_MAX_REDIRECTS if max_redirects is None else max_redirects
        self.max_retries = settings.INGESTION_MAX_RETRIES if max_retries is None else max_retries
        self.respect_robots = settings.INGESTION_RESPECT_ROBOTS_TXT if respect_robots is None else respect_robots
        self.throttle = _DomainThrottle(settings.INGESTION_MIN_DELAY_SECONDS if min_delay is None else min_delay)
        self._sleep = sleep
        self._robots: dict[str, RobotFileParser | None] = {}
        self.client = httpx2.Client(
            transport=transport or GuardedTransport(),
            follow_redirects=False,
            timeout=httpx2.Timeout(settings.INGESTION_TIMEOUT_SECONDS, connect=10.0),
            headers={"User-Agent": settings.INGESTION_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        etag: str = "",
        last_modified: str = "",
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
        item: DiscoveredItem | None = None,
        max_bytes: int | None = None,
    ) -> FetchedDocument:
        """Fetch ``url``. ``max_bytes`` overrides the fetcher's size limit for this call only (sitemaps)."""
        headers = {"Accept": accept}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response, body, final_url = self._request_following_redirects(url, headers, max_bytes or self.max_bytes)
        doc = FetchedDocument(
            item=item or DiscoveredItem(external_id=url, url=url),
            url=final_url,
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=body,
            etag=response.headers.get("etag", "")[:300],
            last_modified=response.headers.get("last-modified", "")[:100],
            not_modified=response.status_code == 304,
            fetched_at=datetime.now(UTC),
        )
        return doc

    # -- internals ---------------------------------------------------------------------------
    def _request_following_redirects(
        self, url: str, headers: dict[str, str], max_bytes: int
    ) -> tuple[httpx2.Response, bytes, str]:
        current = url
        for _ in range(self.max_redirects + 1):
            self.policy.check(current)
            self._check_robots(current)
            response, body = self._request_with_retries(current, headers, max_bytes)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise FetchError("http_error", "redirect without location", response.status_code)
                current = urljoin(current, location)
                continue
            if response.status_code == 304:
                return response, b"", current
            if response.status_code in (404, 410):
                raise FetchError("not_found", f"HTTP {response.status_code}", response.status_code)
            if response.status_code >= 400:
                raise FetchError("http_error", f"HTTP {response.status_code}", response.status_code)
            return response, body, current
        raise FetchError("blocked", "too many redirects")

    def _request_with_retries(self, url: str, headers: dict[str, str], max_bytes: int) -> tuple[httpx2.Response, bytes]:
        host = (urlsplit(url).hostname or "").lower()
        attempt = 0
        while True:
            self.throttle.wait(host)
            try:
                with self.client.stream("GET", url, headers=headers) as response:
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError("too_large", "response exceeds size limit")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError("too_large", "response exceeds size limit")
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except (httpx2.TransportError, httpcore2.ConnectError) as exc:
                if "non-public" in str(exc):
                    raise FetchError("blocked", "destination resolves to a non-public address") from exc
                if attempt >= self.max_retries:
                    raise FetchError("network", f"network error: {type(exc).__name__}") from exc
                self._backoff(attempt, None)
                attempt += 1
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                self._backoff(attempt, response.headers.get("retry-after"))
                attempt += 1
                continue
            return response, body

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(60.0, 2.0**attempt)
        if retry_after:
            try:
                delay = min(300.0, float(retry_after))
            except ValueError:
                with contextlib.suppress(TypeError, ValueError):
                    when = parsedate_to_datetime(retry_after)
                    delay = min(300.0, max(0.0, (when - datetime.now(UTC)).total_seconds()))
        self._sleep(delay)

    def _check_robots(self, url: str) -> None:
        if not self.respect_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            raise FetchError("robots", f"robots.txt unavailable for {origin}; refusing to crawl")
        if not parser.can_fetch(settings.INGESTION_USER_AGENT, url):
            raise FetchError("robots", "disallowed by robots.txt")

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        robots_url = origin + "/robots.txt"
        parser = RobotFileParser(robots_url)
        try:
            self.policy.check(robots_url)
            response, body = self._request_with_retries(robots_url, {"Accept": "text/plain"}, self.max_bytes)
        except FetchError:
            return None
        if response.status_code in (401, 403):
            parser.disallow_all = True
        elif 400 <= response.status_code < 500:
            parser.allow_all = True
        elif response.status_code >= 300:
            return None
        else:
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return parser
