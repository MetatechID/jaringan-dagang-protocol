"""All network boundaries are mocked: these tests never perform DNS or TCP."""
import asyncio
import json
import os
import socket
import sys
from unittest.mock import AsyncMock

import httpcore
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services import partner_callback_url as policy
from services import partner_webhook as webhook


@pytest.mark.parametrize("url", [
    "", " ", "https://example.com/ a", "https://example.com/\n", "http://example.com",
    "//example.com", "https://user:secret@example.com", "https://example.com/#",
    "https://example.com\\@evil.com", "https://example.com:", "https://example.com:0",
    "https://example.com:65536", "https://example.com:bad", "https://-bad.example",
    "https://localhost", "https://bad..example", "https://example.com/%s" % ("a" * 2048),
    *["https://" + host for host in ["127.0.0.1", "10.0.0.1", "169.254.169.254",
        "0.0.0.0", "192.0.2.1", "224.0.0.1", "[::1]", "[fc00::1]", "[fe80::1]",
        "[::ffff:127.0.0.1]", "[fe80::1%25eth0]"]],
])
def test_rejects_unsafe_syntax_and_literals(url):
    with pytest.raises(policy.CallbackURLRejected):
        policy.callback_destination(url)


def answer(ip):
    return (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
            socket.IPPROTO_TCP, "", (ip, 443))


@pytest.mark.parametrize("ips", [["8.8.8.8", "127.0.0.1"], ["::ffff:10.0.0.1"], [], ["169.254.169.254"]])
def test_dns_rejects_entire_unsafe_answer(monkeypatch, ips):
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[answer(ip) for ip in ips]))
        with pytest.raises(policy.CallbackURLRejected):
            await policy.validate_callback_url("https://public.example/hook")
    asyncio.run(run())


def test_dns_timeout(monkeypatch):
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=TimeoutError))
        with pytest.raises(policy.CallbackURLRejected, match="dns_timeout"):
            await policy.validate_callback_url("https://public.example")
    asyncio.run(run())


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, status=200):
        self.status = status
        self.reads = 0
        self.writes = []
        self.sni = None
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        self.reads += 1
        if self.reads > 1:
            raise AssertionError("response body must not be consumed")
        return (f"HTTP/1.1 {self.status} Test\r\nContent-Length: 999999999\r\n"
                "Location: https://127.0.0.1/private\r\n\r\n").encode()

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname
        self.sni = server_hostname
        return self

    def get_extra_info(self, info):
        return None


class Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, status=200):
        self.streams = []
        self.hosts = []
        self.status = status

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append((host, port))
        stream = Stream(self.status)
        self.streams.append(stream)
        return stream


def install_transport(monkeypatch, backend):
    original = policy.CallbackTransport
    monkeypatch.setattr(webhook, "CallbackTransport", lambda: original(backend=backend))
    monkeypatch.setattr(webhook, "_RETRY_DELAYS", (0, 0))


def test_actual_transport_pins_dns_preserves_tls_host_target_and_signed_bytes(monkeypatch):
    backend = Backend()
    install_transport(monkeypatch, backend)
    for key in ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"]:
        monkeypatch.setenv(key, "http://127.0.0.1:9999")
    payload = {"order_id": "placeholder", "event": "payment.paid"}
    async def run():
        dns = AsyncMock(return_value=[answer("8.8.8.8"), answer("1.1.1.1")])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
        assert await webhook.send_payment_callback(
            url="https://public.example:8443/a%2Fb?token=x%2Fy&x=1", secret="secret", payload=payload)
        dns.assert_awaited_once()
    asyncio.run(run())
    assert backend.hosts == [("8.8.8.8", 8443)]
    stream = backend.streams[0]
    assert stream.sni == "public.example"
    assert stream.closed and stream.reads == 1
    raw = b"".join(stream.writes)
    assert b"POST /a%2Fb?token=x%2Fy&x=1 HTTP/1.1" in raw
    assert b"Host: public.example:8443" in raw
    body = json.dumps(payload, separators=(",", ":")).encode()
    assert raw.endswith(body)
    signature = next(line for line in raw.split(b"\r\n") if line.startswith(b"x-oito-signature:"))
    timestamp = int(signature.split(b"t=")[1].split(b",")[0])
    assert webhook.build_signature_header("secret", body, timestamp=timestamp).encode() in signature


def test_write_time_dns_change_is_blocked_at_actual_connection(monkeypatch):
    backend = Backend()
    install_transport(monkeypatch, backend)
    async def run():
        dns = AsyncMock(side_effect=[[answer("8.8.8.8")], [answer("127.0.0.1")],
                                    [answer("10.0.0.1")], [answer("::1")]])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
        await policy.validate_callback_url("https://public.example/hook")
        assert not await webhook.send_payment_callback(url="https://public.example/hook", secret="secret", payload={})
        assert dns.await_count == 4
    asyncio.run(run())
    assert backend.hosts == []


def test_redirect_no_body_no_follow_and_sanitized_logs(monkeypatch, caplog):
    backend = Backend(status=302)
    install_transport(monkeypatch, backend)
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[answer("8.8.8.8")]))
        assert not await webhook.send_payment_callback(
            url="https://public.example/?password=do-not-log", secret="secret", payload={"order_id": "placeholder"})
    asyncio.run(run())
    assert len(backend.hosts) == 3
    assert all(stream.closed and stream.reads == 1 for stream in backend.streams)
    assert "public.example" not in caplog.text and "do-not-log" not in caplog.text
    assert "non_2xx" in caplog.text


@pytest.mark.parametrize("url, expected", [
    ("https://8.8.8.8/a?x=1", ("8.8.8.8", 443)),
    ("https://[2606:4700:4700::1111]/hook", ("2606:4700:4700::1111", 443)),
    ("https://public.example/path?q=a%20b", ("public.example", 443)),
])
def test_public_destinations_remain_supported(url, expected):
    assert policy.callback_destination(url) == expected


def test_raw_transport_error_is_not_logged(monkeypatch, caplog):
    backend = Backend()
    backend.connect_tcp = AsyncMock(side_effect=RuntimeError("secret-url-and-response"))
    install_transport(monkeypatch, backend)
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[answer("8.8.8.8")]))
        assert not await webhook.send_payment_callback(url="https://public.example", secret="secret", payload={})
    asyncio.run(run())
    assert "secret-url-and-response" not in caplog.text
    assert "transport_error" in caplog.text
