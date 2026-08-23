"""Byte-relay proxy endpoint for playback.

Unlike ``/proxy/fast/{video_id}``, this endpoint takes the upstream URL
itself (HMAC-signed by the server at extraction time) and streams it
through to the client without yt-dlp re-extraction or on-disk caching.

Why a separate endpoint:

- ``/proxy/fast/`` re-runs yt-dlp on every request, which (a) re-downloads
  the whole file to ``/downloads/{id}_{itag}.{ext}`` before streaming and
  (b) fails outright when yt-dlp can't extract the video (e.g. ended live
  streams: ``This live event has ended``). For those, Invidious or
  InnerTube may have already returned a perfectly good URL, but the fast
  endpoint throws it away.

- The relay just streams bytes from upstream → client with full Range
  passthrough. Time-to-first-byte is small, seek works, and any URL the
  converter can produce (googlevideo, Invidious /videoplayback, an HLS
  variant manifest) is supported uniformly.

For HLS/DASH manifests, the relay rewrites segment URLs in the body so
segments also flow back through the relay (otherwise individual segments
would still hit googlevideo directly and could 403 for the same
client-IP-mismatch reason that motivated all of this).

``/proxy/fast/`` stays in place — it's still the right shape for
**downloads**, where caching the file on disk is desirable.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
import urllib.parse
from collections import OrderedDict
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

import tokens as token_utils
from egress import is_youtube_url, local_address_for
from routers.proxy._streaming import router
from settings import get_settings
from ytdlp_wrapper import is_safe_url

logger = logging.getLogger(__name__)


DEFAULT_TTL_SECONDS = 6 * 60 * 60

# Default upstream chunk size for the binary stream-and-relay loop. Keep the
# established 10 MiB default for CDNs that support it; the adaptive fallback
# below handles googlevideo URLs that reject larger ranges.
CHUNK_SIZE = 10 * 1024 * 1024

# If an upstream rejects a range with 403, halve the range and retry down to
# this floor. A 403 at the floor is treated as authoritative (expired URL,
# invalid credentials, IP mismatch, etc.) and surfaced normally.
MIN_CHUNK_SIZE = 256 * 1024

# A rejected large range is usually a CDN range-size limit, so reduce it after
# one short pause. If even the minimum range is rejected, retry that exact
# range with bounded exponential backoff in case the CDN is throttling rather
# than treating the URL as permanently invalid.
RANGE_REDUCTION_DELAY_SECONDS = 0.1
HTTP_403_BACKOFF_SECONDS = (0.25, 0.5, 1.0)

# Browsers open several Range requests for one media URL (metadata, playback,
# seeks). Remember a size that actually succeeded so each request does not
# repeat the 10 MiB reduction ladder. The global default remains 10 MiB for
# every new signed URL.
CHUNK_SIZE_CACHE_TTL_SECONDS = 15 * 60
CHUNK_SIZE_CACHE_MAX_ENTRIES = 256
_chunk_size_cache: OrderedDict[str, tuple[int, float]] = OrderedDict()

# After a range still returns 403 at the minimum size and after backoff, the
# signed media URL is not currently usable. Browsers otherwise reopen the same
# failed URL immediately, causing an unbounded retry loop. Short-circuit it
# briefly so the media element receives a terminal error and can stop.
UPSTREAM_403_COOLDOWN_SECONDS = 30.0
_upstream_403_cooldowns: OrderedDict[str, float] = OrderedDict()

# How many times we retry a single chunk before giving up. The retry uses the
# byte offset we've already yielded to the client, so partial progress isn't
# lost — we just resume the upstream Range from where we stopped.
MAX_RETRIES_PER_CHUNK = 3

# Errors that justify a chunk retry. These are typically a TCP RST mid-read
# (googlevideo recycling connections) or a ProtocolError from h2/keep-alive.
# Connect-time errors are also retryable since the new chunk opens a fresh
# connection. We don't retry HTTPStatusError (4xx/5xx) — that's authoritative.
RETRYABLE_UPSTREAM_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.ReadTimeout,
)


def _cached_chunk_size(url: str) -> int:
    cached = _chunk_size_cache.get(url)
    if cached is None:
        return CHUNK_SIZE
    size, stored_at = cached
    if time.monotonic() - stored_at > CHUNK_SIZE_CACHE_TTL_SECONDS:
        _chunk_size_cache.pop(url, None)
        return CHUNK_SIZE
    _chunk_size_cache.move_to_end(url)
    return min(CHUNK_SIZE, max(MIN_CHUNK_SIZE, size))


def _remember_chunk_size(url: str, size: int) -> None:
    now = time.monotonic()
    _chunk_size_cache[url] = (min(CHUNK_SIZE, max(MIN_CHUNK_SIZE, size)), now)
    _chunk_size_cache.move_to_end(url)

    while _chunk_size_cache:
        _, (_, oldest_at) = next(iter(_chunk_size_cache.items()))
        if (
            len(_chunk_size_cache) <= CHUNK_SIZE_CACHE_MAX_ENTRIES
            and now - oldest_at <= CHUNK_SIZE_CACHE_TTL_SECONDS
        ):
            break
        _chunk_size_cache.popitem(last=False)


def _upstream_403_cooldown_remaining(url: str) -> float:
    retry_at = _upstream_403_cooldowns.get(url)
    if retry_at is None:
        return 0.0
    remaining = retry_at - time.monotonic()
    if remaining <= 0:
        _upstream_403_cooldowns.pop(url, None)
        return 0.0
    _upstream_403_cooldowns.move_to_end(url)
    return remaining


def _mark_upstream_403_cooldown(url: str) -> None:
    _upstream_403_cooldowns[url] = time.monotonic() + UPSTREAM_403_COOLDOWN_SECONDS
    _upstream_403_cooldowns.move_to_end(url)
    while len(_upstream_403_cooldowns) > CHUNK_SIZE_CACHE_MAX_ENTRIES:
        _upstream_403_cooldowns.popitem(last=False)

HLS_CONTENT_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl")
DASH_CONTENT_TYPES = ("application/dash+xml",)

# yt-dlp's default UA — what googlevideo expects.
UPSTREAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Headers we accept from the client and forward to upstream.
_FORWARDED_REQUEST_HEADERS = ("range", "if-range", "if-none-match", "if-modified-since")

# Headers we copy from upstream back to the client.
_PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
    "cache-control",
    "expires",
)


def _sign(url: str, exp: int) -> str:
    """HMAC-SHA256 of ``url:exp`` keyed by the server's stream-token secret.

    Same key the existing token system uses (``tokens._get_signing_key``),
    so no new secret to provision.
    """
    payload = f"{url}:{exp}".encode("utf-8")
    digest = hmac.new(token_utils._get_signing_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def _verify(url: str, exp: int, sig: str) -> bool:
    expected = _sign(url, exp)
    return hmac.compare_digest(expected, sig)


def signed_relay_url(
    base_url: str,
    upstream_url: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    content_type: Optional[str] = None,
) -> str:
    """Mint a ``/proxy/relay?...`` URL the client can hit to play this stream.

    The signature gates "only the converter could have produced this URL".
    Without it the relay would be an open HTTP proxy.
    """
    exp = int(time.time()) + ttl_seconds
    sig = _sign(upstream_url, exp)
    params = {"url": upstream_url, "sig": sig, "exp": str(exp)}
    if content_type:
        params["ct"] = content_type
    return f"{base_url.rstrip('/')}/proxy/relay?{urllib.parse.urlencode(params)}"


# --- HLS manifest rewriting -------------------------------------------------

# Lines that are URLs in HLS manifests: either bare lines (segment) or the
# URI="..." attribute on EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA tags.
_HLS_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')


def _rewrite_hls(body: str, base_url: str, manifest_url: str, ttl_seconds: int) -> str:
    """Rewrite every segment / sub-playlist URL in an HLS manifest to a
    fresh signed relay URL.

    `manifest_url` is needed to resolve relative segment paths.
    """

    def absolutize(target: str) -> str:
        return urllib.parse.urljoin(manifest_url, target)

    out_lines = []
    for line in body.splitlines():
        stripped = line.strip()

        # Lines that aren't URLs we just keep as-is...
        if not stripped or stripped.startswith("#"):
            # ...except for tag lines that embed a URI="..." attribute.
            if "URI=" in stripped:
                def _sub(match):
                    signed = signed_relay_url(base_url, absolutize(match.group(2)), ttl_seconds=ttl_seconds)
                    return f"{match.group(1)}{signed}{match.group(3)}"
                line = _HLS_URI_ATTR_RE.sub(_sub, line)
            out_lines.append(line)
            continue

        # Bare URL line (segment or variant playlist)
        out_lines.append(signed_relay_url(base_url, absolutize(stripped), ttl_seconds=ttl_seconds))

    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


# --- DASH manifest rewriting ------------------------------------------------

# DASH manifests use BaseURL elements + SegmentTemplate/SegmentList with
# media="..." / initialization="...". A correct rewrite requires real XML
# parsing and resolution of segment templates. For v1 we leave DASH bodies
# untouched — the iOS client already prefers HLS first, and yt-dlp's DASH
# manifests use absolute googlevideo URLs whose `ip=` may still be bound,
# so this is a known limitation. Logged below so we notice it.

# --- Range / chunked upstream helpers --------------------------------------

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


def _parse_client_range(header: Optional[str]) -> tuple[int, Optional[int]]:
    """Parse a single-range ``Range: bytes=start-end`` (end may be empty).

    Returns ``(start, end_or_None)``. Anything we can't parse falls back to
    ``(0, None)`` — i.e. "stream from the beginning, end at content end".
    Multi-range and suffix-range forms aren't supported (MPV doesn't use
    them; Range parsing for them is more bookkeeping than it's worth).
    """
    if not header:
        return 0, None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return 0, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return start, end


def _parse_content_range_total(header: Optional[str]) -> Optional[int]:
    """Pull the ``/TOTAL`` from ``Content-Range: bytes X-Y/TOTAL``.

    Returns ``None`` if the header is missing or upstream sent ``*`` for the
    total (indicating it doesn't know the full size — happens for chunked
    responses).
    """
    if not header:
        return None
    m = _CONTENT_RANGE_RE.match(header.strip())
    if not m or m.group(3) == "*":
        return None
    return int(m.group(3))


async def _stream_chunked_with_retry(
    client: httpx.AsyncClient,
    url: str,
    base_headers: dict,
    start: int,
    end_inclusive: Optional[int],
):
    """Stream bytes ``start..end_inclusive`` (HTTP-style inclusive end) from
    upstream, breaking the read into ``CHUNK_SIZE``-sized upstream GETs.

    A 403 for an oversized range is retried with progressively smaller chunks.
    Once a smaller size succeeds, it is retained for the rest of the stream.

    Per chunk, retry up to ``MAX_RETRIES_PER_CHUNK`` times on transient
    connection errors. The retry resumes from the byte offset already
    yielded, so the client sees a contiguous stream regardless of upstream
    flakiness — bytes already delivered are never delivered twice.

    If ``end_inclusive`` is ``None`` we keep going until upstream returns
    fewer bytes than requested (signalling end-of-content) or returns a
    non-success status.
    """
    cursor = start
    chunk_size = _cached_chunk_size(url)

    while end_inclusive is None or cursor <= end_inclusive:
        chunk_end = cursor + chunk_size - 1
        if end_inclusive is not None:
            chunk_end = min(chunk_end, end_inclusive)

        chunk_yielded = 0
        floor_403_retries = 0
        while True:
            last_error: Optional[BaseException] = None
            range_rejected = False

            for attempt in range(MAX_RETRIES_PER_CHUNK):
                attempt_start = cursor + chunk_yielded
                req_headers = {**base_headers, "Range": f"bytes={attempt_start}-{chunk_end}"}

                try:
                    resp = await client.send(
                        client.build_request("GET", url, headers=req_headers),
                        stream=True,
                    )
                except RETRYABLE_UPSTREAM_ERRORS as e:
                    last_error = e
                    logger.warning(
                        f"[Relay] connect retry {attempt + 1}/{MAX_RETRIES_PER_CHUNK} "
                        f"for bytes={attempt_start}-{chunk_end}: {e}"
                    )
                    continue

                # 416 Range Not Satisfiable when end_inclusive was unknown
                # means we walked past content end — clean exit.
                if resp.status_code == 416 and end_inclusive is None:
                    await resp.aclose()
                    return

                requested_size = chunk_end - attempt_start + 1
                if resp.status_code == 403 and chunk_yielded == 0 and requested_size > MIN_CHUNK_SIZE:
                    await resp.aread()
                    await resp.aclose()
                    reduced_size = max(MIN_CHUNK_SIZE, requested_size // 2)
                    chunk_size = min(chunk_size, reduced_size)
                    chunk_end = cursor + chunk_size - 1
                    if end_inclusive is not None:
                        chunk_end = min(chunk_end, end_inclusive)
                    logger.warning(
                        f"[Relay] upstream rejected a {requested_size}-byte range with 403; "
                        f"retrying with {chunk_size}-byte chunks after "
                        f"{RANGE_REDUCTION_DELAY_SECONDS:.2f}s"
                    )
                    await asyncio.sleep(RANGE_REDUCTION_DELAY_SECONDS)
                    range_rejected = True
                    break

                if resp.status_code == 403 and chunk_yielded == 0 and (
                    floor_403_retries < len(HTTP_403_BACKOFF_SECONDS)
                ):
                    await resp.aread()
                    await resp.aclose()
                    delay = HTTP_403_BACKOFF_SECONDS[floor_403_retries]
                    floor_403_retries += 1
                    logger.warning(
                        f"[Relay] upstream rejected minimum range bytes={attempt_start}-{chunk_end}; "
                        f"retry {floor_403_retries}/{len(HTTP_403_BACKOFF_SECONDS)} after {delay:.2f}s"
                    )
                    await asyncio.sleep(delay)
                    range_rejected = True
                    break

                if resp.status_code >= 400:
                    # Authoritative error from upstream, surface to client.
                    body = await resp.aread()
                    await resp.aclose()
                    if resp.status_code == 403:
                        _mark_upstream_403_cooldown(url)
                    raise httpx.HTTPStatusError(
                        f"upstream {resp.status_code} for bytes={attempt_start}-{chunk_end}: {body[:200]!r}",
                        request=resp.request,
                        response=resp,
                    )

                # If we asked for a Range and upstream answered 200, it
                # doesn't honour Range — we'd be re-downloading the whole
                # body from byte 0 for every chunk. Stream this body once.
                single_shot = resp.status_code == 200
                if chunk_yielded == 0:
                    _remember_chunk_size(url, chunk_size)
                try:
                    async for piece in resp.aiter_raw():
                        chunk_yielded += len(piece)
                        yield piece
                    await resp.aclose()
                    break
                except RETRYABLE_UPSTREAM_ERRORS as e:
                    last_error = e
                    logger.warning(
                        f"[Relay] read retry {attempt + 1}/{MAX_RETRIES_PER_CHUNK} "
                        f"after {chunk_yielded}B of bytes={cursor}-{chunk_end}: {e}"
                    )
                    await resp.aclose()
                    continue
            else:
                # Exhausted transient-error retries.
                raise last_error if last_error else RuntimeError("relay chunk failed")

            if range_rejected:
                continue
            break

        # Defensive: if upstream returned no body, don't loop forever.
        if chunk_yielded == 0:
            return

        chunk_request_size = chunk_end - cursor + 1
        cursor += chunk_yielded

        # Single-shot mode (upstream ignored Range) — we just streamed the
        # whole body, anything more would be duplicates.
        if single_shot:
            return

        # If we asked for N bytes and got fewer, upstream is signalling EOF —
        # stop even if end_inclusive was beyond the real content end.
        if chunk_yielded < chunk_request_size:
            return


# --- Endpoint ---------------------------------------------------------------


@router.get("/relay")
async def relay(
    request: Request,
    url: str,
    sig: str,
    exp: int,
    ct: Optional[str] = None,
):
    if int(time.time()) > exp:
        raise HTTPException(status_code=403, detail="Relay URL expired")

    if not _verify(url, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid relay signature")

    # SSRF guard. The signature already prevents arbitrary URLs from
    # reaching here, but if the converter ever produced a URL pointing at
    # an internal service we still want to refuse.
    if not is_safe_url(url):
        raise HTTPException(status_code=403, detail="URL targets restricted network resources")

    cooldown_remaining = _upstream_403_cooldown_remaining(url)
    if cooldown_remaining > 0:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream media URL is cooling down after repeated HTTP 403 responses; retry in "
            f"{cooldown_remaining:.1f}s",
        )

    upstream_headers = {
        "User-Agent": UPSTREAM_USER_AGENT,
        # Don't let httpx negotiate gzip/br upstream — we relay raw bytes and
        # would have to either pass Content-Encoding through or decompress.
        # For media this is overwhelmingly the right call: video bytes are
        # not re-compressible anyway, and avoiding it sidesteps a class of
        # passthrough bugs.
        "Accept-Encoding": "identity",
    }
    # Forward client conditionals; the client's Range is parsed/applied
    # below per-chunk, so don't pass it raw to the meta probe.
    for h in _FORWARDED_REQUEST_HEADERS:
        if h == "range":
            continue
        v = request.headers.get(h)
        if v is not None:
            upstream_headers[h] = v

    client_range = request.headers.get("range")
    range_start, range_end = _parse_client_range(client_range)

    # For YouTube-family hosts, honor the egress proxy / forced IP family.
    # Other upstreams (LAN Invidious, generic sites) keep the plain client —
    # an explicit transport would also disable HTTP(S)_PROXY env mounts.
    # proxy and local_addr are never both set: effective_ip_family() returns
    # "auto" while the proxy is active (httpx drops local_address otherwise).
    transport = None
    if is_youtube_url(url):
        s = get_settings()
        proxy = s.effective_yt_egress_proxy()
        local_addr = local_address_for(s.effective_ip_family())
        if proxy or local_addr:
            transport = httpx.AsyncHTTPTransport(proxy=proxy, local_address=local_addr)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        follow_redirects=True,
        **({"transport": transport} if transport else {}),
    )

    # --- Meta probe: tiny first request to discover total/status/type ----
    # We use Range: bytes=0-0 to get just the first byte (1 B body) so we
    # can read Content-Range/total without downloading anything significant.
    # The actual payload is fetched fresh via _stream_chunked_with_retry
    # below; this connection is closed before streaming starts.
    meta_headers = {**upstream_headers, "Range": "bytes=0-0"}
    try:
        meta_req = client.build_request("GET", url, headers=meta_headers)
        meta = await client.send(meta_req, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        logger.warning(f"[Relay] Upstream connect failed for {url[:120]}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream connect failed: {e}") from e

    if meta.status_code >= 400:
        status = meta.status_code
        await meta.aread()
        await meta.aclose()
        await client.aclose()
        logger.warning(f"[Relay] upstream rejected metadata probe with HTTP {status}")
        raise HTTPException(status_code=502, detail=f"Upstream media URL returned HTTP {status}")

    upstream_content_type = meta.headers.get("content-type", "")
    response_content_type = (ct or upstream_content_type or "").split(";")[0].strip().lower()
    is_hls = response_content_type in HLS_CONTENT_TYPES or url.split("?", 1)[0].endswith(".m3u8")
    is_dash = response_content_type in DASH_CONTENT_TYPES or url.split("?", 1)[0].endswith(".mpd")

    # --- Manifest path: buffer + rewrite + return one-shot --------------
    # HLS manifests are tiny — just refetch in full (the meta probe only got
    # 1 byte), then rewrite segment URLs. Don't chunk; don't retry; don't
    # try to be clever.
    if is_hls and meta.status_code in (200, 206):
        await meta.aclose()
        try:
            full = await client.get(url, headers={**upstream_headers})
            text = full.text
            base_url_self = f"{request.url.scheme}://{request.url.netloc}"
            rewritten = _rewrite_hls(text, base_url=base_url_self, manifest_url=url, ttl_seconds=DEFAULT_TTL_SECONDS)
            headers = {"content-type": "application/vnd.apple.mpegurl"}
            for h in ("cache-control", "etag", "last-modified"):
                v = full.headers.get(h)
                if v:
                    headers[h] = v
            return StreamingResponse(
                iter([rewritten.encode("utf-8")]),
                status_code=full.status_code,
                headers=headers,
            )
        finally:
            await client.aclose()

    if is_dash:
        # Known limitation — pass through with a warning. See note above.
        logger.info(f"[Relay] DASH manifest passthrough (segments not rewritten): {url[:120]}")

    # --- Binary path: chunked stream-and-relay ---------------------------
    total = _parse_content_range_total(meta.headers.get("content-range"))
    if total is None:
        # Fallback: maybe upstream sent Content-Length on a 200 (no Range
        # support). Use that.
        cl = meta.headers.get("content-length")
        if cl and meta.status_code == 200:
            try:
                total = int(cl)
            except ValueError:
                pass

    # Decide what to send back to the client. If it asked for a Range and
    # we know the total, return 206 + Content-Range; otherwise 200 + length.
    if client_range and total is not None:
        loop_end = range_end if range_end is not None else (total - 1)
        # Clamp range_end to content end if client asked for more than exists.
        loop_end = min(loop_end, total - 1)
        response_status = 206
        response_headers = {
            "content-range": f"bytes {range_start}-{loop_end}/{total}",
            "accept-ranges": "bytes",
        }
        # Content-Range still tells the media client the requested span and
        # total size. Deliberately leave this fallible streamed response
        # without Content-Length: if upstream disappears mid-range, fixed
        # framing makes Uvicorn raise a second, misleading "content shorter"
        # exception after the useful relay warning.
    elif total is not None:
        loop_end = total - 1
        response_status = 200
        response_headers = {
            "content-length": str(total),
            "accept-ranges": "bytes",
        }
    else:
        # Total unknown (chunked upstream, or the meta probe failed to give
        # us one). Stream open-ended; let _stream_chunked_with_retry stop
        # when upstream signals EOF via 416 / short read.
        loop_end = range_end
        response_status = 206 if client_range else (meta.status_code or 200)
        response_headers = {"accept-ranges": "bytes"}

    # Copy over a few content-classification headers from upstream.
    for h in ("content-type", "etag", "last-modified", "cache-control", "expires"):
        v = meta.headers.get(h)
        if v:
            response_headers[h] = v
    if ct:
        response_headers["content-type"] = ct

    await meta.aclose()

    async def body_iter():
        try:
            async for piece in _stream_chunked_with_retry(
                client=client,
                url=url,
                base_headers=upstream_headers,
                start=range_start,
                end_inclusive=loop_end,
            ):
                yield piece
        except httpx.HTTPStatusError as e:
            logger.warning(f"[Relay] upstream error mid-stream: {e}")
        except RETRYABLE_UPSTREAM_ERRORS as e:
            logger.warning(f"[Relay] gave up mid-stream after retries: {e}")
        finally:
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=response_status,
        headers=response_headers,
    )
