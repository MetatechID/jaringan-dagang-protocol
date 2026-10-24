"""Callback-only HTTPS policy and DNS-pinned transport.

Resolve at connect time, reject the entire answer set if any address is unsafe,
and pass only a numeric IP to the network backend. The HTTP origin is never
rewritten: httpcore retains the original Host, TLS SNI and certificate name.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpcore
import httpx

MAX_URL_LENGTH = 2048
DNS_TIMEOUT_SECONDS = 3.0


class CallbackURLRejected(ValueError):
    """Fixed reason codes only; never include a URL or resolver exception."""


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise CallbackURLRejected("invalid_address") from None
    mapped = getattr(address, "ipv4_mapped", None)
    if (not address.is_global or address.is_multicast
            or (mapped is not None and (not mapped.is_global or mapped.is_multicast))):
        raise CallbackURLRejected("non_public_address")
    return str(address)


def callback_destination(url: str) -> tuple[str, int]:
    """Validate syntax without normalizing away suspicious input or URL data."""
    if (not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or "\\" in url or "#" in url):
        raise CallbackURLRejected("invalid_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CallbackURLRejected("invalid_url") from None
    if (parsed.scheme != "https" or not parsed.netloc or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.netloc.endswith(":")):
        raise CallbackURLRejected("invalid_url")
    port = 443 if port is None else port
    if not 1 <= port <= 65535 or "%" in host:
        raise CallbackURLRejected("invalid_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise CallbackURLRejected("invalid_host") from None
        labels = ascii_host.rstrip(".").split(".")
        if (len(ascii_host) > 253 or len(labels) < 2
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in labels)):
            raise CallbackURLRejected("invalid_host")
        host = ascii_host
    else:
        host = _public_ip(host)
    return host, port


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                ),
                timeout=DNS_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise CallbackURLRejected("dns_timeout") from None
        except OSError:
            raise CallbackURLRejected("dns_failed") from None
        if not answers:
            raise CallbackURLRejected("dns_empty")
        return list(dict.fromkeys(_public_ip(answer[4][0]) for answer in answers))
    return [_public_ip(host)]


async def validate_callback_url(url: str) -> None:
    host, port = callback_destination(url)
    await resolve_public_addresses(host, port)


class PinnedCallbackBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None):
        self.backend = backend if backend is not None else httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await resolve_public_addresses(host, port)
        # No fallback to the hostname: even a later DNS change cannot change
        # the destination of this connection. The returned stream is unchanged.
        return await self.backend.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise CallbackURLRejected("unix_socket_forbidden")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class CallbackTransport(httpx.AsyncBaseTransport):
    """Small public-API adapter; no proxies, redirects or HTTPX pool mutation."""

    def __init__(self, *, backend: httpcore.AsyncNetworkBackend | None = None):
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=PinnedCallbackBackend(backend),
            max_connections=1, max_keepalive_connections=0, retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        callback_destination(str(request.url))
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme, host=request.url.raw_host,
                port=request.url.port, target=request.url.raw_path,
            ),
            headers=request.headers.raw, content=request.stream,
            extensions=request.extensions,
        ))
        return httpx.Response(
            response.status, headers=response.headers,
            stream=_ResponseStream(response.stream), extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()
