"""InnerTube browse endpoint - trending, popular, channel tabs.

Uses YouTube's InnerTube /browse endpoint to fetch categorized content.
"""

import logging
from typing import Any, Dict, List, Optional

from innertube._client import InnerTubeError, _build_context, _get_client_version, innertube_post
from innertube._converters import (
    _parse_count_text,
    grid_playlist_to_invidious,
    grid_video_to_invidious,
    rich_item_to_invidious,
    video_renderer_to_invidious,
)

logger = logging.getLogger("innertube")

# Channel tab param tokens (base64-encoded protobuf)
# These tell the /browse endpoint which tab to show
CHANNEL_TAB_PARAMS = {
    "videos": "EgZ2aWRlb3PyBgQKAjoA",
    "shorts": "EgZzaG9ydHPyBgUKA5oBAA%3D%3D",
    "streams": "EgdzdHJlYW1z8gYECgJ6AA%3D%3D",
    "playlists": "EglwbGF5bGlzdHPyBgQKAkIA",
}

# YouTube removed the old aggregated FEtrending page, so requests to
# browseId=FEtrending now return HTTP 400. Invidious resolved this by using
# the still-available channel-backed trending shelves (livestreams as default,
# gaming for the gaming category).
TRENDING_BROWSE_PARAMS = {
    "default": ("UC4R8DWoMoI7CAwX8_LjQHig", "EgdsaXZldGFikgEDCKEK"),
    "livestreams": ("UC4R8DWoMoI7CAwX8_LjQHig", "EgdsaXZldGFikgEDCKEK"),
    "gaming": ("UCOpNcN46UbXVtpKMrmU4Abg", "Egh0cmVuZGluZw%3D%3D"),
}


# Cache of resolved @handle -> UC... channel IDs
_handle_cache: Dict[str, str] = {}


async def _resolve_browse_id(channel_id: str) -> str:
    """Resolve a channel handle (@name) to a UC... browse ID.

    YouTube's /browse endpoint no longer accepts handles as browseId
    (returns HTTP 400), so handles must be resolved via
    navigation/resolve_url first. Resolved IDs are cached in-process.
    """
    if not channel_id.startswith("@"):
        return channel_id

    cached = _handle_cache.get(channel_id)
    if cached:
        return cached

    data = await innertube_post(
        "navigation/resolve_url",
        {"url": f"https://www.youtube.com/{channel_id}"},
    )
    browse_id = data.get("endpoint", {}).get("browseEndpoint", {}).get("browseId", "")
    if not browse_id.startswith("UC"):
        raise InnerTubeError(f"Could not resolve handle {channel_id} to a channel ID")

    _handle_cache[channel_id] = browse_id
    logger.info(f"[InnerTube] Resolved handle {channel_id} -> {browse_id}")
    return browse_id


def _extract_items_from_tab(data: Dict[str, Any]) -> tuple[List[Dict], Optional[str]]:
    """Extract video/playlist items and continuation from a browse response tab."""
    items = []
    continuation = None

    # Navigate the response structure
    # Path: contents -> twoColumnBrowseResultsRenderer -> tabs[] -> tabRenderer -> content
    tabs = (
        data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
    )

    for tab in tabs:
        tab_renderer = tab.get("tabRenderer", {})
        tab_content = tab_renderer.get("content", {})

        # richGridRenderer (trending, channel videos)
        rich_grid = tab_content.get("richGridRenderer", {})
        if rich_grid:
            for item in rich_grid.get("contents", []):
                if "richItemRenderer" in item:
                    converted = rich_item_to_invidious(item["richItemRenderer"])
                    if converted:
                        items.append(converted)
                elif "continuationItemRenderer" in item:
                    token = (
                        item["continuationItemRenderer"]
                        .get("continuationEndpoint", {})
                        .get("continuationCommand", {})
                        .get("token")
                    )
                    if token:
                        continuation = token

        # sectionListRenderer (trending page uses this)
        section_list = tab_content.get("sectionListRenderer", {})
        if section_list:
            for section in section_list.get("contents", []):
                section_renderer = section.get("itemSectionRenderer", {})
                for content in section_renderer.get("contents", []):
                    if "shelfRenderer" in content:
                        shelf_content = content["shelfRenderer"].get("content", {})
                        shelf_items = shelf_content.get("expandedShelfContentsRenderer", {}).get("items", [])
                        if not shelf_items:
                            shelf_items = shelf_content.get("gridRenderer", {}).get("items", [])
                        for shelf_item in shelf_items:
                            if "videoRenderer" in shelf_item:
                                items.append(video_renderer_to_invidious(shelf_item["videoRenderer"]))
                            elif "gridVideoRenderer" in shelf_item:
                                items.append(grid_video_to_invidious(shelf_item["gridVideoRenderer"]))
                    elif "videoRenderer" in content:
                        items.append(video_renderer_to_invidious(content["videoRenderer"]))
                    elif "gridVideoRenderer" in content:
                        items.append(grid_video_to_invidious(content["gridVideoRenderer"]))
                    elif "gridPlaylistRenderer" in content:
                        items.append(grid_playlist_to_invidious(content["gridPlaylistRenderer"]))

                # Check for continuation in section
                if "continuationItemRenderer" in section:
                    token = (
                        section["continuationItemRenderer"]
                        .get("continuationEndpoint", {})
                        .get("continuationCommand", {})
                        .get("token")
                    )
                    if token:
                        continuation = token

    return items, continuation


def _extract_items_from_continuation(data: Dict[str, Any]) -> tuple[List[Dict], Optional[str]]:
    """Extract items from a continuation response."""
    items = []
    continuation = None

    for action in data.get("onResponseReceivedActions", []):
        append_action = action.get("appendContinuationItemsAction", {})
        for item in append_action.get("continuationItems", []):
            if "richItemRenderer" in item:
                converted = rich_item_to_invidious(item["richItemRenderer"])
                if converted:
                    items.append(converted)
            elif "gridVideoRenderer" in item:
                items.append(grid_video_to_invidious(item["gridVideoRenderer"]))
            elif "gridPlaylistRenderer" in item:
                items.append(grid_playlist_to_invidious(item["gridPlaylistRenderer"]))
            elif "videoRenderer" in item:
                items.append(video_renderer_to_invidious(item["videoRenderer"]))
            elif "continuationItemRenderer" in item:
                token = (
                    item["continuationItemRenderer"]
                    .get("continuationEndpoint", {})
                    .get("continuationCommand", {})
                    .get("token")
                )
                if token:
                    continuation = token

    return items, continuation


async def get_trending(region: str = "US", trending_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get trending videos from YouTube.

    Args:
        region: Region code (e.g., "US", "GB", "DE")

    Returns:
        List of video dicts in Invidious format
    """
    version = await _get_client_version()
    browse_id, params = TRENDING_BROWSE_PARAMS.get(
        (trending_type or "default").lower(), TRENDING_BROWSE_PARAMS["default"]
    )
    body = {
        "browseId": browse_id,
        "params": params,
        "context": _build_context(region=region, client_version=version),
    }
    data = await innertube_post("browse", body)
    items, _ = _extract_items_from_tab(data)
    logger.info(
        f"[InnerTube] Trending: got {len(items)} videos for region={region}, type={trending_type or 'default'}"
    )
    return items


async def get_popular() -> List[Dict[str, Any]]:
    """Get popular videos from YouTube.

    YouTube merged popular into trending, so this returns trending content.
    """
    return await get_trending()


async def get_channel_videos(
    channel_id: str, continuation: Optional[str] = None
) -> Dict[str, Any]:
    """Get channel videos tab.

    Args:
        channel_id: YouTube channel ID (UC...) or handle (@name)
        continuation: Continuation token for pagination

    Returns:
        Dict with "videos" list and optional "continuation" token
    """
    if continuation:
        body = {"continuation": continuation}
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_continuation(data)
    else:
        body = {
            "browseId": await _resolve_browse_id(channel_id),
            "params": CHANNEL_TAB_PARAMS["videos"],
        }
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_tab(data)

    logger.info(f"[InnerTube] Channel videos {channel_id}: got {len(items)} videos")
    return {"videos": items, "continuation": next_cont}


async def get_channel_shorts(
    channel_id: str, continuation: Optional[str] = None
) -> Dict[str, Any]:
    """Get channel shorts tab."""
    if continuation:
        body = {"continuation": continuation}
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_continuation(data)
    else:
        body = {
            "browseId": await _resolve_browse_id(channel_id),
            "params": CHANNEL_TAB_PARAMS["shorts"],
        }
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_tab(data)

    return {"videos": items, "continuation": next_cont}


async def get_channel_streams(
    channel_id: str, continuation: Optional[str] = None
) -> Dict[str, Any]:
    """Get channel past live streams tab."""
    if continuation:
        body = {"continuation": continuation}
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_continuation(data)
    else:
        body = {
            "browseId": await _resolve_browse_id(channel_id),
            "params": CHANNEL_TAB_PARAMS["streams"],
        }
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_tab(data)

    return {"videos": items, "continuation": next_cont}


async def get_channel_playlists(
    channel_id: str, continuation: Optional[str] = None
) -> Dict[str, Any]:
    """Get channel playlists tab."""
    if continuation:
        body = {"continuation": continuation}
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_continuation(data)
    else:
        body = {
            "browseId": await _resolve_browse_id(channel_id),
            "params": CHANNEL_TAB_PARAMS["playlists"],
        }
        data = await innertube_post("browse", body)
        items, next_cont = _extract_items_from_tab(data)

    return {"playlists": items, "continuation": next_cont}


async def get_channel_info(channel_id: str) -> Optional[Dict[str, Any]]:
    """Get channel metadata (banners, avatars, description, sub count) via InnerTube.

    Args:
        channel_id: YouTube channel ID (UC...) or handle (@name)

    Returns:
        Dict with channel metadata, or None if not available
    """
    body = {"browseId": await _resolve_browse_id(channel_id)}
    data = await innertube_post("browse", body)

    # Extract from pageHeaderViewModel
    phvm = (
        data.get("header", {})
        .get("pageHeaderRenderer", {})
        .get("content", {})
        .get("pageHeaderViewModel", {})
    )
    if not phvm:
        return None

    # Channel name from metadata.title or title field
    author = data.get("metadata", {}).get("channelMetadataRenderer", {}).get("title", "")
    if not author:
        title_obj = phvm.get("title", {}).get("dynamicTextViewModel", {})
        author = title_obj.get("text", {}).get("content", "")

    # Resolved channel ID from metadata
    resolved_id = data.get("metadata", {}).get("channelMetadataRenderer", {}).get("externalId", channel_id)

    # Banners
    banners = []
    banner_sources = (
        phvm.get("banner", {})
        .get("imageBannerViewModel", {})
        .get("image", {})
        .get("sources", [])
    )
    for src in banner_sources:
        banners.append({
            "url": src.get("url", ""),
            "width": src.get("width"),
            "height": src.get("height"),
        })

    # Avatars
    thumbnails = []
    avatar_sources = (
        phvm.get("image", {})
        .get("decoratedAvatarViewModel", {})
        .get("avatar", {})
        .get("avatarViewModel", {})
        .get("image", {})
        .get("sources", [])
    )
    for src in avatar_sources:
        thumbnails.append({
            "url": src.get("url", ""),
            "width": src.get("width"),
            "height": src.get("height"),
        })

    # Description
    description = (
        phvm.get("description", {})
        .get("descriptionPreviewViewModel", {})
        .get("description", {})
        .get("content", "")
    )

    # Parse metadata rows for subscriber count and verified status
    sub_count = None
    verified = False
    metadata_rows = (
        phvm.get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
    )
    for row in metadata_rows:
        for part in row.get("metadataParts", []):
            text = part.get("text", {}).get("content", "")
            if "subscriber" in text.lower():
                sub_count = _parse_count_text(text)

    # Check for verification badge — scan the response JSON for ownerBadges
    # YouTube puts channel verification badges on video renderers, not in the header
    import json as _json

    response_text = _json.dumps(data)
    if "BADGE_STYLE_TYPE_VERIFIED" in response_text:
        verified = True

    logger.info(f"[InnerTube] Channel info {channel_id}: {author}, {len(banners)} banners, {len(thumbnails)} avatars")

    return {
        "authorId": resolved_id,
        "author": author,
        "description": description or None,
        "subCount": sub_count,
        "authorThumbnails": thumbnails,
        "authorBanners": banners,
        "authorVerified": verified,
    }
