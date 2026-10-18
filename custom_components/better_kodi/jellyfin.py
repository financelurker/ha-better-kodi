"""Jellyfin items from Home Assistant's media browser.

Home Assistant's Jellyfin integration hands players a stream URL such as
``http://jellyfin:8096/Videos/<id>/stream?static=true&api_key=…``. Kodi labels
it "stream", and Kodi's JSON-RPC API has no way to pass a title along. So:

1. If Kodi has the item in its own library (synced by Jellyfin for Kodi), the
   library item is queued. Then Kodi itself knows title and artwork, and every
   app (Cast Kodi, Yatse, …) shows them.
2. Otherwise, with Jellyfin for Kodi installed, its play path is queued, so
   playback is reported to Jellyfin (watched state, resume).
3. Otherwise the stream URL is queued as before.

In every case the title is read from Jellyfin, for Better Kodi's own queue.
"""

from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from .kodi_client import KodiClient, KodiError

_LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
ADDON_JELLYFIN = "plugin.video.jellyfin"
STREAM_PATH = re.compile(r"^(?P<prefix>.*)/Videos/(?P<id>[0-9a-fA-F]{32})/stream(?:\.\w+)?$")

# Jellyfin item type -> (Kodi library method, result key, Kodi id key)
KODI_LIBRARY = {
    "Movie": ("VideoLibrary.GetMovies", "movies", "movieid"),
    "Episode": ("VideoLibrary.GetEpisodes", "episodes", "episodeid"),
    "MusicVideo": ("VideoLibrary.GetMusicVideos", "musicvideos", "musicvideoid"),
}


@dataclass(slots=True, frozen=True)
class JellyfinStream:
    """A Jellyfin video stream URL, taken apart."""

    server: str
    item_id: str
    api_key: str | None


@dataclass(slots=True, frozen=True)
class JellyfinItem:
    """What Better Kodi needs to know about a Jellyfin item."""

    item_id: str
    kind: str
    title: str
    thumbnail: str
    path: str | None


def parse_stream_url(url: str) -> JellyfinStream | None:
    """Recognize a Jellyfin video stream URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (match := STREAM_PATH.match(parts.path)) is None:
        return None
    query = {key.lower(): values[0] for key, values in parse_qs(parts.query).items()}
    server = f"{parts.scheme}://{parts.netloc}{match.group('prefix')}"
    return JellyfinStream(server, match.group("id").lower(), query.get("api_key"))


def display_title(item: dict[str, Any]) -> str:
    """Return a readable title, such as "Show - S01E02 - Episode" or "Movie (2024)"."""
    name = item.get("Name") or ""
    if item.get("Type") == "Episode":
        parts = [item["SeriesName"]] if item.get("SeriesName") else []
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if season is not None and episode is not None:
            parts.append(f"S{season:02d}E{episode:02d}")
        return " – ".join([*parts, name]) if name else " – ".join(parts)
    if year := item.get("ProductionYear"):
        return f"{name} ({year})"
    return name


async def async_get_item(
    session: aiohttp.ClientSession, stream: JellyfinStream
) -> JellyfinItem | None:
    """Read an item from the Jellyfin server named in a stream URL."""
    if not stream.api_key:
        return None
    try:
        async with session.get(
            f"{stream.server}/Items",
            params={"ids": stream.item_id, "fields": "Path", "api_key": stream.api_key},
            timeout=HTTP_TIMEOUT,
        ) as response:
            if response.status != 200:
                _LOGGER.debug("Jellyfin answered %s for item %s", response.status, stream.item_id)
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Could not read Jellyfin item %s: %s", stream.item_id, err)
        return None
    items = (data or {}).get("Items") if isinstance(data, dict) else None
    if not items:
        return None
    raw = items[0]
    return JellyfinItem(
        item_id=stream.item_id,
        kind=raw.get("Type") or "",
        title=display_title(raw),
        # Jellyfin serves images without a login; no API key in this URL.
        thumbnail=f"{stream.server}/Items/{stream.item_id}/Images/Primary?maxWidth=400",
        path=raw.get("Path"),
    )


async def async_find_in_kodi_library(
    client: KodiClient, item: JellyfinItem
) -> dict[str, int] | None:
    """Return the Kodi library item for a Jellyfin item, if Kodi has it.

    Jellyfin for Kodi (add-on mode) stores files as
    ``plugin://plugin.video.jellyfin/<library>/?filename=…&id=<jellyfin id>&…``.
    With direct paths it stores the real file; then the file name has to match
    and be unique.
    """
    if (library := KODI_LIBRARY.get(item.kind)) is None:
        return None
    method, result_key, id_key = library
    by_id = await _library_search(client, method, result_key, "contains", f"id={item.item_id}")
    if len(by_id) == 1:
        return {id_key: by_id[0][id_key]}
    if item.path:
        file_name = re.split(r"[\\/]", item.path)[-1]
        by_name = await _library_search(client, method, result_key, "is", file_name)
        if len(by_name) == 1:
            return {id_key: by_name[0][id_key]}
    return None


async def _library_search(
    client: KodiClient, method: str, result_key: str, operator: str, value: str
) -> list[dict[str, Any]]:
    try:
        result = await client.call(
            method,
            {
                "filter": {"field": "filename", "operator": operator, "value": value},
                "limits": {"start": 0, "end": 2},
            },
        )
    except KodiError as err:
        _LOGGER.debug("Kodi library search %s failed: %s", method, err)
        return []
    return list((result or {}).get(result_key) or [])


def addon_path(item_id: str) -> str:
    """Return the Jellyfin for Kodi play path of an item."""
    return f"plugin://{ADDON_JELLYFIN}/?" + urlencode({"mode": "play", "id": item_id})
