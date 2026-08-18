"""Tests for the byte-relay proxy's range handling."""

import time

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import routers.proxy._relay as relay_module


@pytest.fixture(autouse=True)
def clear_chunk_size_cache():
    relay_module._chunk_size_cache.clear()
    relay_module._upstream_403_cooldowns.clear()
    yield
    relay_module._chunk_size_cache.clear()
    relay_module._upstream_403_cooldowns.clear()


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


def _range_bounds(request: httpx.Request) -> tuple[int, int]:
    value = request.headers["range"].removeprefix("bytes=")
    start, end = value.split("-", 1)
    return int(start), int(end)


async def test_stream_uses_safe_chunk_size():
    requested_ranges = []
    total = relay_module.CHUNK_SIZE + 7

    def handler(request: httpx.Request) -> httpx.Response:
        start, end = _range_bounds(request)
        requested_ranges.append((start, end))
        return httpx.Response(206, stream=_AsyncBytes(b"x" * (end - start + 1)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = b"".join(
            [
                piece
                async for piece in relay_module._stream_chunked_with_retry(
                    client, "https://example.com/video", {}, 0, total - 1
                )
            ]
        )

    assert len(body) == total
    assert requested_ranges == [
        (0, relay_module.CHUNK_SIZE - 1),
        (relay_module.CHUNK_SIZE, total - 1),
    ]


async def test_stream_reduces_chunk_size_after_403(monkeypatch):
    monkeypatch.setattr(relay_module, "CHUNK_SIZE", 8)
    monkeypatch.setattr(relay_module, "MIN_CHUNK_SIZE", 2)
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(relay_module.asyncio, "sleep", record_sleep)
    requested_ranges = []

    def handler(request: httpx.Request) -> httpx.Response:
        start, end = _range_bounds(request)
        requested_ranges.append((start, end))
        size = end - start + 1
        if size > 4:
            return httpx.Response(403)
        return httpx.Response(206, stream=_AsyncBytes(b"x" * size))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = b"".join(
            [
                piece
                async for piece in relay_module._stream_chunked_with_retry(
                    client, "https://example.com/video", {}, 0, 9
                )
            ]
        )

    assert body == b"x" * 10
    assert requested_ranges == [(0, 7), (0, 3), (4, 7), (8, 9)]
    assert delays == [relay_module.RANGE_REDUCTION_DELAY_SECONDS]


async def test_stream_backs_off_when_minimum_range_is_temporarily_rejected(monkeypatch):
    monkeypatch.setattr(relay_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(relay_module, "MIN_CHUNK_SIZE", 4)
    monkeypatch.setattr(relay_module, "HTTP_403_BACKOFF_SECONDS", (0.25, 0.5))
    delays = []
    attempts = 0

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(relay_module.asyncio, "sleep", record_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(403)
        start, end = _range_bounds(request)
        return httpx.Response(206, stream=_AsyncBytes(b"x" * (end - start + 1)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = b"".join(
            [
                piece
                async for piece in relay_module._stream_chunked_with_retry(
                    client, "https://example.com/transient-video", {}, 0, 3
                )
            ]
        )

    assert body == b"xxxx"
    assert attempts == 3
    assert delays == [0.25, 0.5]


async def test_stream_reuses_learned_chunk_size_for_same_url(monkeypatch):
    monkeypatch.setattr(relay_module, "CHUNK_SIZE", 8)
    monkeypatch.setattr(relay_module, "MIN_CHUNK_SIZE", 2)
    requested_ranges = []

    async def skip_sleep(_delay):
        pass

    monkeypatch.setattr(relay_module.asyncio, "sleep", skip_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        start, end = _range_bounds(request)
        requested_ranges.append((start, end))
        size = end - start + 1
        if size > 4:
            return httpx.Response(403)
        return httpx.Response(206, stream=_AsyncBytes(b"x" * size))

    url = "https://example.com/cached-video"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = b"".join(
            [
                piece
                async for piece in relay_module._stream_chunked_with_retry(client, url, {}, 0, 7)
            ]
        )
        second = b"".join(
            [
                piece
                async for piece in relay_module._stream_chunked_with_retry(client, url, {}, 8, 15)
            ]
        )

    assert first == b"x" * 8
    assert second == b"x" * 8
    assert requested_ranges == [(0, 7), (0, 3), (4, 7), (8, 11), (12, 15)]


async def test_stream_cools_down_url_after_terminal_403(monkeypatch):
    monkeypatch.setattr(relay_module, "CHUNK_SIZE", 4)
    monkeypatch.setattr(relay_module, "MIN_CHUNK_SIZE", 4)
    monkeypatch.setattr(relay_module, "HTTP_403_BACKOFF_SECONDS", (0.25, 0.5))

    async def skip_sleep(_delay):
        pass

    monkeypatch.setattr(relay_module.asyncio, "sleep", skip_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    url = "https://example.com/terminal-video"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            b"".join(
                [
                    piece
                    async for piece in relay_module._stream_chunked_with_retry(client, url, {}, 0, 3)
                ]
            )

    assert relay_module._upstream_403_cooldown_remaining(url) > 0


async def test_relay_short_circuits_url_during_403_cooldown(monkeypatch):
    url = "https://example.com/cooling-video"
    relay_module._mark_upstream_403_cooldown(url)
    monkeypatch.setattr(relay_module, "_verify", lambda *_args: True)
    monkeypatch.setattr(relay_module, "is_safe_url", lambda _url: True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/proxy/relay",
            "query_string": b"",
            "headers": [],
            "server": ("127.0.0.1", 8085),
        }
    )

    with pytest.raises(HTTPException) as raised:
        await relay_module.relay(request, url, "valid", int(time.time()) + 60)

    assert raised.value.status_code == 502
    assert "cooling down" in raised.value.detail


async def test_relay_returns_502_when_metadata_probe_is_rejected(monkeypatch):
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=0-0"
        return httpx.Response(403)

    client = real_async_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(relay_module.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(relay_module, "_verify", lambda *_args: True)
    monkeypatch.setattr(relay_module, "is_safe_url", lambda _url: True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/proxy/relay",
            "query_string": b"",
            "headers": [],
            "server": ("127.0.0.1", 8085),
        }
    )

    with pytest.raises(HTTPException) as raised:
        await relay_module.relay(request, "https://example.com/video", "valid", int(time.time()) + 60)

    assert raised.value.status_code == 502
    assert raised.value.detail == "Upstream media URL returned HTTP 403"
    assert client.is_closed


async def test_relay_range_uses_content_range_without_fixed_framing(monkeypatch):
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=0-0"
        return httpx.Response(
            206,
            headers={
                "content-range": "bytes 0-0/100",
                "content-type": "video/mp4",
            },
            stream=_AsyncBytes(b"x"),
        )

    client = real_async_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(relay_module.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(relay_module, "_verify", lambda *_args: True)
    monkeypatch.setattr(relay_module, "is_safe_url", lambda _url: True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/proxy/relay",
            "query_string": b"",
            "headers": [(b"range", b"bytes=10-19")],
            "server": ("127.0.0.1", 8085),
        }
    )

    response = await relay_module.relay(
        request,
        "https://example.com/video",
        "valid",
        int(time.time()) + 60,
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 10-19/100"
    assert "content-length" not in response.headers
    await client.aclose()
