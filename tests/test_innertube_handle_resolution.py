"""Regression tests for channel handle resolution in InnerTube browse calls.

YouTube's /browse endpoint stopped accepting @handles as browseId (HTTP 400),
so handles must be resolved to UC... IDs via navigation/resolve_url first.
"""

import asyncio
import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import innertube._browse as browse_mod
from innertube._client import InnerTubeError


@pytest.fixture(autouse=True)
def clear_handle_cache():
    browse_mod._handle_cache.clear()
    yield
    browse_mod._handle_cache.clear()


def test_uc_id_passes_through_without_network(monkeypatch):
    async def fail_post(endpoint, body, **kwargs):
        raise AssertionError("resolve should not hit the network for UC ids")

    monkeypatch.setattr(browse_mod, "innertube_post", fail_post)

    result = asyncio.run(browse_mod._resolve_browse_id("UCBJycsmduvYEL83R_U4JriQ"))
    assert result == "UCBJycsmduvYEL83R_U4JriQ"


def test_handle_resolves_via_resolve_url_and_caches(monkeypatch):
    calls = []

    async def fake_post(endpoint, body, **kwargs):
        calls.append((endpoint, body))
        return {
            "endpoint": {
                "browseEndpoint": {"browseId": "UCBJycsmduvYEL83R_U4JriQ"}
            }
        }

    monkeypatch.setattr(browse_mod, "innertube_post", fake_post)

    result = asyncio.run(browse_mod._resolve_browse_id("@MKBHD"))
    assert result == "UCBJycsmduvYEL83R_U4JriQ"
    assert calls[0][0] == "navigation/resolve_url"
    assert calls[0][1]["url"] == "https://www.youtube.com/@MKBHD"

    # Second call is served from the cache
    result = asyncio.run(browse_mod._resolve_browse_id("@MKBHD"))
    assert result == "UCBJycsmduvYEL83R_U4JriQ"
    assert len(calls) == 1


def test_unresolvable_handle_raises(monkeypatch):
    async def fake_post(endpoint, body, **kwargs):
        return {"endpoint": {}}

    monkeypatch.setattr(browse_mod, "innertube_post", fake_post)

    with pytest.raises(InnerTubeError):
        asyncio.run(browse_mod._resolve_browse_id("@doesnotexist"))


def test_get_channel_videos_browses_with_resolved_id(monkeypatch):
    calls = []

    async def fake_post(endpoint, body, **kwargs):
        calls.append((endpoint, body))
        if endpoint == "navigation/resolve_url":
            return {
                "endpoint": {
                    "browseEndpoint": {"browseId": "UCBJycsmduvYEL83R_U4JriQ"}
                }
            }
        return {}

    monkeypatch.setattr(browse_mod, "innertube_post", fake_post)

    asyncio.run(browse_mod.get_channel_videos("@MKBHD"))

    browse_calls = [body for endpoint, body in calls if endpoint == "browse"]
    assert browse_calls[0]["browseId"] == "UCBJycsmduvYEL83R_U4JriQ"
