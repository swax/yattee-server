"""Tests for selecting and merging adaptive download formats."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from converters._ytdlp import ytdlp_to_video_response
from routers.proxy._fast_download import (
    _active_downloads,
    _merged_container,
    fast_download,
    run_ytdlp_download,
)


def test_merged_container_prefers_native_containers():
    assert _merged_container({"ext": "mp4"}, {"ext": "m4a"}) == "mp4"
    assert _merged_container({"ext": "webm"}, {"ext": "opus"}) == "webm"
    assert _merged_container({"ext": "mp4"}, {"ext": "webm"}) == "mkv"


def test_direct_stream_response_includes_authenticated_download_token(mock_ytdlp_video):
    with patch("converters._ytdlp.token_utils.generate_stream_token", return_value="signed-download-token"):
        response = ytdlp_to_video_response(
            mock_ytdlp_video,
            proxy_streams=False,
            user_id=42,
        )

    assert response.downloadToken == "signed-download-token"
    assert response.adaptiveFormats[0].url.startswith("https://example.com/")


@pytest.mark.asyncio
async def test_paired_formats_start_merged_download(mock_ytdlp_video, tmp_path):
    async_download = AsyncMock()

    _active_downloads.clear()
    try:
        with (
            patch("routers.proxy._fast_download.DOWNLOADS_DIR", tmp_path),
            patch(
                "routers.proxy._fast_download.get_video_info",
                new=AsyncMock(return_value=mock_ytdlp_video),
            ),
            patch("routers.proxy._fast_download.validate_proxy_token"),
            patch("routers.proxy._fast_download._rate_limited_download", new=async_download),
        ):
            response = await fast_download(
                "dQw4w9WgXcQ",
                request=None,
                video_itag="137",
                audio_itag="140",
            )
            await asyncio.sleep(0)

        async_download.assert_awaited_once()
        args = async_download.await_args.args
        kwargs = async_download.await_args.kwargs
        assert args[1] == "137+140"
        assert Path(args[2]).name == "dQw4w9WgXcQ_137_140.mp4"
        assert kwargs["merge_output_format"] == "mp4"
        assert response.media_type == "video/mp4"
    finally:
        _active_downloads.clear()


@pytest.mark.asyncio
async def test_paired_formats_require_video_and_audio_roles(mock_ytdlp_video):
    with (
        patch(
            "routers.proxy._fast_download.get_video_info",
            new=AsyncMock(return_value=mock_ytdlp_video),
        ),
        patch("routers.proxy._fast_download.validate_proxy_token"),
    ):
        with pytest.raises(HTTPException, match="video_itag must identify a video-only format") as exc_info:
            await fast_download(
                "dQw4w9WgXcQ",
                request=None,
                video_itag="140",
                audio_itag="137",
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_run_ytdlp_download_adds_merge_arguments(tmp_path):
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"", b""))
    output_path = tmp_path / "merged.mp4"

    with (
        patch("credentials.get_credentials_for_url", new=AsyncMock(return_value=([], []))),
        patch(
            "routers.proxy._fast_download.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process,
    ):
        await run_ytdlp_download(
            "dQw4w9WgXcQ",
            "137+140",
            output_path,
            "dQw4w9WgXcQ_137_140",
            merge_output_format="mp4",
        )

    command = list(create_process.await_args.args)
    assert command[command.index("-f") + 1] == "137+140"
    assert command[command.index("--merge-output-format") + 1] == "mp4"
    assert "--force-overwrites" in command
    assert command[command.index("-o") + 1] == str(output_path)
