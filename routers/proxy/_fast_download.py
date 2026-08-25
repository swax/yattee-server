"""Fast download endpoint and all supporting functions."""

import asyncio
import logging
import time
from pathlib import Path

import aiofiles
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from routers.proxy._auth import validate_proxy_token
from routers.proxy._cleanup import (
    DOWNLOADS_DIR,
    _active_downloads,
    _downloads_lock,
    get_download_semaphore,
)
from routers.proxy._streaming import router
from security import sanitize_command_for_logging
from settings import get_settings
from ytdlp_wrapper import (
    YtDlpError,
    extract_url,
    get_video_info,
    is_safe_url,
    is_valid_url,
    sanitize_extension,
    sanitize_format_id,
    ytdlp_network_args,
)

logger = logging.getLogger(__name__)


def _find_format_by_itag(formats: list[dict], itag: str) -> dict | None:
    """Find an extracted format, accepting yt-dlp's suffixed itag variants."""
    for fmt in formats:
        format_id = str(fmt.get("format_id", ""))
        if format_id == itag or format_id.startswith(f"{itag}-"):
            return fmt
    return None


def _merged_container(video_format: dict, audio_format: dict) -> str:
    """Choose a lossless output container compatible with both selected streams."""
    video_ext = str(video_format.get("ext") or "").lower()
    audio_ext = str(audio_format.get("ext") or "").lower()

    if video_ext == "mp4" and audio_ext in {"m4a", "mp4"}:
        return "mp4"
    if video_ext == "webm" and audio_ext in {"webm", "opus"}:
        return "webm"
    return "mkv"


def _video_content_type(ext: str) -> str:
    """Return the correct media type for a merged video container."""
    return {
        "mkv": "video/x-matroska",
        "mp4": "video/mp4",
        "webm": "video/webm",
    }.get(ext, f"video/{ext}")


# =============================================================================
# Fast Download Endpoint - Uses yt-dlp parallel downloading
# =============================================================================


async def run_ytdlp_download(
    video_id: str,
    itag: str,
    output_path: Path,
    download_key: str,
    video_url: str = None,
    max_retries: int = 3,
    merge_output_format: str = None,
):
    """Run yt-dlp download in background and track progress.

    Includes retry logic for transient failures (e.g., 403 errors from
    sites like Dailymotion).
    """
    import credentials

    temp_files = []
    try:
        # Determine URL - use provided URL for external sites, or YouTube format
        url = video_url or f"https://www.youtube.com/watch?v={video_id}"

        # Get credentials for this URL
        cred_args = []
        try:
            cred_args, temp_files = await credentials.get_credentials_for_url(url)
        except (ValueError, KeyError, TypeError, OSError) as e:
            logger.warning(f"[FastDownload] Failed to get credentials: {e}")

        # Build yt-dlp command with resilience options for long downloads
        s = get_settings()
        cmd = [
            s.ytdlp_path,
            *ytdlp_network_args(s),  # Egress proxy + forced IP family
            *cred_args,  # Include credential args (cookies, etc.)
            "-f",
            itag,
            "--no-part",  # Don't use .part files
            "--no-mtime",  # Don't set file modification time
            "--socket-timeout",
            "30",  # Longer socket timeout
            "--retries",
            "10",  # More retries on network errors
            "--fragment-retries",
            "10",  # Retry individual fragments
            "--retry-sleep",
            "linear=1::5",  # Linear backoff 1-5s between retries
            *(["--merge-output-format", merge_output_format, "--force-overwrites"] if merge_output_format else []),
            "-o",
            str(output_path),
            url,
        ]

        logger.info(f"[FastDownload] Starting yt-dlp: {sanitize_command_for_logging(cmd)}")

        # Retry loop for transient failures (like Dailymotion 403 errors)
        last_error = None
        for attempt in range(max_retries):
            if attempt > 0:
                wait_time = 2 ** (attempt - 1)  # Exponential backoff: 1s, 2s
                logger.info(f"[FastDownload] Retry {attempt}/{max_retries} after {wait_time}s wait for {video_id}")
                await asyncio.sleep(wait_time)
                # Remove partial file if it exists from failed attempt
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass

            # Run yt-dlp as subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            async with _downloads_lock:
                if download_key in _active_downloads:
                    _active_downloads[download_key]["process"] = process

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=600,  # 10 minute timeout for yt-dlp
                )
            except asyncio.TimeoutError:
                logger.error(f"[FastDownload] yt-dlp timed out after 600s: {video_id} ({itag})")
                process.kill()
                await process.wait()
                async with _downloads_lock:
                    if download_key in _active_downloads:
                        _active_downloads[download_key]["error"] = "Download timed out"
                # Clean up temp files
                if temp_files:
                    credentials.cleanup_temp_files(temp_files)
                return

            if process.returncode == 0:
                logger.info(f"[FastDownload] Completed: {video_id} ({itag})")
                async with _downloads_lock:
                    if download_key in _active_downloads:
                        _active_downloads[download_key]["complete"] = True
                # Clean up temp credential files on success
                if temp_files:
                    credentials.cleanup_temp_files(temp_files)
                return  # Success, exit function

            # Failed - check if it's a retryable error
            last_error = stderr.decode()
            if "403" in last_error or "No video formats found" in last_error:
                logger.warning(
                    f"[FastDownload] Attempt {attempt + 1}/{max_retries} failed with retryable error: "
                    f"{last_error[:200]}"
                )
                continue  # Retry
            else:
                # Non-retryable error, break out of loop
                break

        # All retries exhausted or non-retryable error
        logger.error(f"[FastDownload] Failed after {max_retries} attempts: {video_id} ({itag}) - {last_error}")
        async with _downloads_lock:
            if download_key in _active_downloads:
                _active_downloads[download_key]["error"] = last_error

        # Clean up temp credential files
        if temp_files:
            credentials.cleanup_temp_files(temp_files)

    except (OSError, YtDlpError, ValueError) as e:
        logger.error(f"[FastDownload] Exception: {video_id} ({itag}) - {e}")
        async with _downloads_lock:
            if download_key in _active_downloads:
                _active_downloads[download_key]["error"] = str(e)
        # Clean up temp files on error too
        if temp_files:
            credentials.cleanup_temp_files(temp_files)


async def _rate_limited_download(
    video_id: str,
    itag: str,
    output_path: Path,
    download_key: str,
    video_url: str = None,
    merge_output_format: str = None,
):
    """Run download with rate limiting via semaphore."""
    async with get_download_semaphore():
        await run_ytdlp_download(
            video_id,
            itag,
            output_path,
            download_key,
            video_url,
            merge_output_format=merge_output_format,
        )


async def stream_file_as_it_downloads(file_path: Path, download_key: str, expected_size: int):
    """Stream a file as it's being downloaded by yt-dlp."""
    bytes_sent = 0
    chunk_size = 256 * 1024  # 256KB chunks
    stall_count = 0
    # Merged downloads do not create their final output file until both source
    # streams have downloaded and FFmpeg starts muxing them. Match the yt-dlp
    # subprocess timeout, plus a small cushion, before treating that as a stall.
    max_stalls = 6600  # 11 minutes (6600 * 0.1s)

    while True:
        # Check download status
        async with _downloads_lock:
            download_info = _active_downloads.get(download_key, {})
            is_complete = download_info.get("complete", False)
            has_error = download_info.get("error")

        if has_error:
            logger.error(f"[FastDownload] Stream error: {has_error}")
            break

        # Check current file size
        try:
            current_size = file_path.stat().st_size if file_path.exists() else 0
        except OSError:
            current_size = 0

        # Read available data
        if current_size > bytes_sent:
            try:
                async with aiofiles.open(file_path, "rb") as f:
                    await f.seek(bytes_sent)
                    while bytes_sent < current_size:
                        to_read = min(chunk_size, current_size - bytes_sent)
                        chunk = await f.read(to_read)
                        if chunk:
                            bytes_sent += len(chunk)
                            stall_count = 0  # Reset stall counter
                            yield chunk
                        else:
                            break
            except OSError as e:
                logger.error(f"[FastDownload] Read error: {e}")
                await asyncio.sleep(0.1)
                continue

        # Check if download is complete and we've sent everything
        if is_complete and bytes_sent >= current_size:
            logger.info(f"[FastDownload] Stream complete: {bytes_sent} bytes")
            break

        # Wait for more data
        stall_count += 1
        if stall_count > max_stalls:
            logger.error(
                f"[FastDownload] Stream stalled for too long (sent {bytes_sent} bytes, expected {expected_size})"
            )
            # Mark download as stalled so subsequent requests will restart it
            async with _downloads_lock:
                if download_key in _active_downloads:
                    _active_downloads[download_key]["error"] = f"Stream stalled after {bytes_sent} bytes"
            break

        await asyncio.sleep(0.1)


@router.get("/fast/{video_id}")
async def fast_download(
    video_id: str,
    request: Request,
    itag: str = None,
    video_itag: str = None,
    audio_itag: str = None,
    format: str = "best",
    url: str = None,
    token: str = None,
):
    """
    Fast download using yt-dlp's parallel downloading.

    This endpoint starts yt-dlp in the background and streams the file
    as it downloads, giving you yt-dlp's download speed benefits.

    Args:
        video_id: YouTube video ID (or external site video ID)
        itag: Specific format itag to use
        video_itag: Video-only format to merge with audio_itag
        audio_itag: Audio-only format to merge with video_itag
        format: Format selector (best, bestvideo, bestaudio)
        url: Original URL for external sites (required for non-YouTube)
        token: Streaming token (required when basic auth is enabled)
    """
    logger.info(
        f"[FastDownload] Request received: video_id={video_id}, itag={itag}, "
        f"video_itag={video_itag}, audio_itag={audio_itag}, format={format}"
    )

    if bool(video_itag) != bool(audio_itag):
        raise HTTPException(status_code=400, detail="video_itag and audio_itag must be provided together")

    # SSRF prevention - validate URL if provided
    if url:
        if not is_valid_url(url):
            raise HTTPException(status_code=400, detail="Invalid URL format")
        if not is_safe_url(url):
            raise HTTPException(status_code=403, detail="URL targets restricted network resources")

    # Validate token if basic auth is enabled
    validate_proxy_token(token, video_id)

    try:
        # Get video info - use URL for external sites, video_id for YouTube
        if url:
            # External site - use extract_url with the original URL
            info = await extract_url(url, use_cache=False)  # Don't cache to get fresh stream URLs
        else:
            # YouTube - use video_id
            info = await get_video_info(video_id)
        formats = info.get("formats", [])

        merge_output_format = None
        if video_itag and audio_itag:
            # Paired requests losslessly mux one video-only and one audio-only
            # stream. Resolve IDs from fresh metadata before constructing the
            # yt-dlp selector so untrusted query text never reaches the command.
            selected_video = _find_format_by_itag(formats, video_itag)
            selected_audio = _find_format_by_itag(formats, audio_itag)

            if not selected_video or not selected_audio:
                raise HTTPException(status_code=404, detail="Selected video or audio format was not found")
            if selected_video.get("vcodec") in {None, "none"} or selected_video.get("acodec") != "none":
                raise HTTPException(status_code=400, detail="video_itag must identify a video-only format")
            if selected_audio.get("vcodec") != "none" or selected_audio.get("acodec") in {None, "none"}:
                raise HTTPException(status_code=400, detail="audio_itag must identify an audio-only format")

            video_format_id = str(selected_video.get("format_id", ""))
            audio_format_id = str(selected_audio.get("format_id", ""))
            format_id = f"{video_format_id}+{audio_format_id}"
            ext = _merged_container(selected_video, selected_audio)
            filesize = sum(
                int(fmt.get("filesize") or fmt.get("filesize_approx") or 0) for fmt in (selected_video, selected_audio)
            )

            try:
                safe_video_id = sanitize_format_id(video_format_id)
                safe_audio_id = sanitize_format_id(audio_format_id)
                safe_format_id = f"{safe_video_id}_{safe_audio_id}"
                safe_ext = sanitize_extension(ext)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            content_type = _video_content_type(safe_ext)
            merge_output_format = safe_ext
        else:
            # Preserve the existing single-format download behavior.
            selected_format = None
            if itag:
                selected_format = _find_format_by_itag(formats, itag)

                # External-site format IDs can change between extractions.
                if not selected_format and url:
                    logger.warning(f"[FastDownload] No exact itag match for {itag}, falling back to quality matching")
                    logger.debug(f"[FastDownload] Available formats: {[f.get('format_id') for f in formats]}")
                    is_video_format = itag.endswith("v") or not itag.endswith("a")

                    if is_video_format:
                        video_formats = [f for f in formats if f.get("vcodec") != "none"]
                        if video_formats:
                            selected_format = max(
                                video_formats, key=lambda x: (x.get("height", 0) or 0, x.get("tbr", 0) or 0)
                            )
                    else:
                        audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
                        if audio_formats:
                            selected_format = max(audio_formats, key=lambda x: x.get("abr", 0) or 0)

                    if selected_format:
                        logger.info(
                            f"[FastDownload] Quality fallback selected format: {selected_format.get('format_id')}"
                        )
            elif format == "bestaudio":
                audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
                if audio_formats:
                    selected_format = max(audio_formats, key=lambda x: x.get("abr", 0) or 0)
            elif format == "bestvideo":
                video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
                if video_formats:
                    selected_format = max(video_formats, key=lambda x: (x.get("height", 0) or 0, x.get("vbr", 0) or 0))
            else:
                muxed_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
                if muxed_formats:
                    selected_format = max(muxed_formats, key=lambda x: (x.get("height", 0) or 0, x.get("tbr", 0) or 0))

            if not selected_format:
                raise HTTPException(status_code=404, detail="No suitable format found")

            format_id = str(selected_format.get("format_id", ""))
            ext = selected_format.get("ext", "mp4")
            filesize = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0

            try:
                safe_format_id = sanitize_format_id(format_id)
                safe_ext = sanitize_extension(ext)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            if selected_format.get("vcodec") != "none":
                content_type = f"video/{safe_ext}"
            else:
                content_type = f"audio/{safe_ext}"

        download_key = f"{video_id}_{safe_format_id}"
        output_path = DOWNLOADS_DIR / f"{download_key}.{safe_ext}"

        # Defense in depth: verify output_path stays within DOWNLOADS_DIR
        try:
            resolved_output = output_path.resolve()
            resolved_downloads = DOWNLOADS_DIR.resolve()
            if not str(resolved_output).startswith(str(resolved_downloads)):
                logger.error(f"[FastDownload] Path traversal attempt detected: {output_path}")
                raise HTTPException(status_code=400, detail="Invalid file path")
        except (OSError, ValueError) as e:
            logger.error(f"[FastDownload] Path resolution error: {e}")
            raise HTTPException(status_code=400, detail="Invalid file path")

        # Check if download is already in progress or complete
        async with _downloads_lock:
            existing = _active_downloads.get(download_key)
            if existing:
                # Check if we should reuse or restart the download
                has_error = existing.get("error")
                is_complete = existing.get("complete", False)

                # Check if file exists and has content
                try:
                    file_exists = output_path.exists() and output_path.stat().st_size > 0
                except OSError:
                    file_exists = False

                # Invalidate cache if:
                # 1. Download has error, OR
                # 2. Download is "complete" but file is missing/empty (previous stream consumed it), OR
                # 3. Download is in-progress but file doesn't exist and started >60s ago (stale)
                start_time = existing.get("start_time", 0)
                is_stale = not is_complete and not file_exists and (time.time() - start_time > 60)

                if has_error or (is_complete and not file_exists) or is_stale:
                    logger.info(
                        f"[FastDownload] Invalidating download: {download_key} "
                        f"(error={has_error is not None}, complete={is_complete}, "
                        f"file_exists={file_exists}, stale={is_stale})"
                    )
                    del _active_downloads[download_key]
                    existing = None
                else:
                    logger.info(f"[FastDownload] Reusing existing download: {download_key}")

            if not existing:
                # Start new download
                _active_downloads[download_key] = {
                    "video_id": video_id,
                    "format_id": format_id,
                    "path": output_path,
                    "complete": False,
                    "error": None,
                    "process": None,
                    "start_time": time.time(),
                }

                # Start yt-dlp download in background (rate-limited)
                asyncio.create_task(
                    _rate_limited_download(
                        video_id,
                        format_id,
                        output_path,
                        download_key,
                        video_url=url,
                        merge_output_format=merge_output_format,
                    )
                )
                logger.info(f"[FastDownload] Started new download: {download_key}")

        # Build response headers
        response_headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "none",  # Don't support range requests for streaming downloads
        }

        # Send estimated size in custom header (not Content-Length!)
        # Content-Length can't be used because yt-dlp's metadata filesize may differ
        # from actual downloaded size, causing "Response content shorter than Content-Length" errors
        if filesize > 0:
            response_headers["X-Expected-Content-Length"] = str(filesize)

        return StreamingResponse(
            stream_file_as_it_downloads(output_path, download_key, filesize),
            headers=response_headers,
            media_type=content_type,
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except YtDlpError as e:
        raise HTTPException(status_code=404, detail=f"Video not found: {e}")
    except (KeyError, TypeError, OSError) as e:
        logger.error(f"[FastDownload] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
