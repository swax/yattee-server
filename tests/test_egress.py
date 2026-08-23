"""Tests for egress.py - YouTube host scoping and IP family helpers."""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from egress import is_youtube_url, local_address_for


class TestIsYoutubeUrl:
    """Tests for is_youtube_url host scoping."""

    def test_googlevideo_stream_host(self):
        assert is_youtube_url("https://rr1---sn-abc123.googlevideo.com/videoplayback?expire=1") is True

    def test_youtube_host(self):
        assert is_youtube_url("https://www.youtube.com/watch?v=abc") is True

    def test_youtu_be(self):
        assert is_youtube_url("https://youtu.be/abc") is True

    def test_thumbnail_hosts(self):
        assert is_youtube_url("https://i.ytimg.com/vi/abc/hq720.jpg") is True
        assert is_youtube_url("https://yt3.ggpht.com/some/avatar") is True

    def test_bare_suffix_host(self):
        assert is_youtube_url("https://googlevideo.com/x") is True

    def test_case_insensitive(self):
        assert is_youtube_url("https://WWW.YouTube.COM/watch?v=abc") is True

    def test_host_with_port(self):
        assert is_youtube_url("https://www.youtube.com:443/watch?v=abc") is True

    def test_lookalike_host_rejected(self):
        """Suffix match must be boundary-safe."""
        assert is_youtube_url("https://evilgooglevideo.com/videoplayback") is False
        assert is_youtube_url("https://notyoutube.com/watch") is False

    def test_lan_invidious_rejected(self):
        assert is_youtube_url("http://invidious.lan/videoplayback?id=abc") is False
        assert is_youtube_url("http://192.168.1.10:3000/videoplayback") is False

    def test_generic_site_rejected(self):
        assert is_youtube_url("https://vimeo.com/12345") is False

    def test_garbage_input(self):
        assert is_youtube_url("") is False
        assert is_youtube_url("not a url") is False
        assert is_youtube_url("relative/path") is False


class TestLocalAddressFor:
    """Tests for family -> httpx local_address mapping."""

    def test_ipv6(self):
        assert local_address_for("ipv6") == "::"

    def test_ipv4(self):
        assert local_address_for("ipv4") == "0.0.0.0"

    def test_auto(self):
        assert local_address_for("auto") is None
