"""Tests for the core yt-dlp subprocess wrapper."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import MockProcess
from ytdlp_wrapper._core import run_ytdlp


@pytest.mark.asyncio
async def test_run_ytdlp_uses_webpo_capable_mweb_client():
    """YouTube extraction must use the client supported by the PO-token provider."""
    runtime_settings = SimpleNamespace(
        ytdlp_path="yt-dlp",
        ytdlp_timeout=30,
        effective_yt_egress_proxy=lambda: None,
        effective_ip_family=lambda: "auto",
    )

    with (
        patch.dict(os.environ, {"YATTEE_PO_TOKEN_PROVIDER": "1"}),
        patch("ytdlp_wrapper._core.get_settings", return_value=runtime_settings),
        patch(
            "credentials.get_credentials_for_url",
            new=AsyncMock(return_value=([], [])),
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=MockProcess(stdout="{}")),
        ) as create_subprocess,
    ):
        await run_ytdlp("-J", "https://www.youtube.com/watch?v=8zh0ouiYIZc")

    command = create_subprocess.await_args.args
    assert command[:4] == (
        "yt-dlp",
        "--extractor-args",
        "youtube:player_client=mweb",
        "-J",
    )
    assert command[-2:] == ("--", "https://www.youtube.com/watch?v=8zh0ouiYIZc")


@pytest.mark.asyncio
async def test_run_ytdlp_does_not_force_mweb_without_managed_provider():
    """Other deployment paths keep their existing yt-dlp client selection."""
    runtime_settings = SimpleNamespace(
        ytdlp_path="yt-dlp",
        ytdlp_timeout=30,
        effective_yt_egress_proxy=lambda: None,
        effective_ip_family=lambda: "auto",
    )

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("ytdlp_wrapper._core.get_settings", return_value=runtime_settings),
        patch(
            "credentials.get_credentials_for_url",
            new=AsyncMock(return_value=([], [])),
        ),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=MockProcess(stdout="{}")),
        ) as create_subprocess,
    ):
        await run_ytdlp("-J", "https://vimeo.com/123456")

    command = create_subprocess.await_args.args
    assert "youtube:player_client=mweb" not in command
