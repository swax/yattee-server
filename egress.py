"""Egress policy helpers for YouTube-bound traffic.

The yt_egress_proxy and yt_ip_family settings apply only to YouTube-family
hosts at the HTTP-client layer (the relay also fetches from Invidious —
possibly on the LAN — and from generic extraction sites, where forcing a
proxy or IP family would break connectivity).
"""

import urllib.parse
from typing import Optional

YOUTUBE_HOST_SUFFIXES = (
    "googlevideo.com",
    "youtube.com",
    "ytimg.com",
    "ggpht.com",
    "youtu.be",
)


def is_youtube_url(url: str) -> bool:
    """True if the URL's host belongs to the YouTube/googlevideo family."""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in YOUTUBE_HOST_SUFFIXES)


def local_address_for(family: str) -> Optional[str]:
    """Map an IP family setting to an httpx local_address bind.

    Binding to the wildcard address of one family makes connect attempts on
    the other family fail, which is the httpx way to force a family.
    """
    if family == "ipv6":
        return "::"
    if family == "ipv4":
        return "0.0.0.0"
    return None
