"""Media-source backend for Jellyfin items from Home Assistant's media browser.

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

from ..kodi_client import KodiClient, KodiError
from .base import BackendContext, BackendItem, MediaSourceBackend

_LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
ADDON_JELLYFIN = "plugin.video.jellyfin"
PATH_PARTS_TO_MATCH = 2  # file name and its folder
# Jellyfin item IDs: 32 hex digits, sometimes written as a GUID with dashes.
_ITEM_ID = r"(?P<id>[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
# Links to one item's media: what Home Assistant's Jellyfin integration hands
# players (…/Videos/<id>/stream), HLS (…/master.m3u8), and "Copy stream URL"
# or download links of the Jellyfin apps (…/Items/<id>/Download).
STREAM_PATH = re.compile(
    rf"^(?P<prefix>.*?)(?:/Videos/{_ITEM_ID}/(?:stream(?:\.\w+)?|master\.m3u8|main\.m3u8)"
    rf"|/Items/{_ITEM_ID.replace('?P<id>', '?P<id2>')}/Download)$",
    re.IGNORECASE,
)

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
    """Recognize a Jellyfin stream or download URL of one item."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (match := STREAM_PATH.match(parts.path)) is None:
        return None
    query = {key.lower(): values[0] for key, values in parse_qs(parts.query).items()}
    server = f"{parts.scheme}://{parts.netloc}{match.group('prefix')}"
    # Jellyfin accepts "ApiKey"; "api_key" only while legacy authorization is on.
    api_key = query.get("apikey") or query.get("api_key")
    item_id = (match.group("id") or match.group("id2")).replace("-", "").lower()
    return JellyfinStream(server, item_id, api_key)


def display_title(item: dict[str, Any]) -> str:
    """Return a readable title, such as "Show - S01E02 - Episode" or "Movie (2024)"."""
    name = item.get("Name") or ""
    if item.get("Type") == "Episode":
        parts = [item["SeriesName"]] if item.get("SeriesName") else []
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if isinstance(season, int) and isinstance(episode, int):
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
            params={"ids": stream.item_id, "fields": "Path"},
            # Header instead of query, so the key never appears in a URL.
            headers={"Authorization": f'MediaBrowser Token="{stream.api_key}"'},
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
        # Kodi's "filename" field is the file name only. File names like
        # "S01E01.mkv" repeat across shows, so the folder must match too.
        jellyfin_parts = _path_parts(item.path)
        by_name = await _library_search(
            client, method, result_key, "is", jellyfin_parts[-1], properties=["file"]
        )
        tail = jellyfin_parts[-PATH_PARTS_TO_MATCH:]
        matches = [
            found
            for found in by_name
            if _path_parts(found.get("file", ""))[-PATH_PARTS_TO_MATCH:] == tail
        ]
        if len(matches) == 1:
            return {id_key: matches[0][id_key]}
    return None


def _path_parts(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]", path) if part]


async def _library_search(
    client: KodiClient,
    method: str,
    result_key: str,
    operator: str,
    value: str,
    *,
    properties: list[str] | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "filter": {"field": "filename", "operator": operator, "value": value},
        "limits": {"start": 0, "end": 20},
    }
    if properties:
        params["properties"] = properties
    try:
        result = await client.call(method, params)
    except KodiError as err:
        _LOGGER.debug("Kodi library search %s failed: %s", method, err)
        return []
    return list((result or {}).get(result_key) or [])


def addon_path(item_id: str) -> str:
    """Return the Jellyfin for Kodi play path of an item."""
    return f"plugin://{ADDON_JELLYFIN}/?" + urlencode({"mode": "play", "id": item_id})


class JellyfinBackend(MediaSourceBackend):
    """Jellyfin, through Home Assistant's Jellyfin integration."""

    name = "jellyfin"

    def recognizes(self, url: str) -> bool:
        """Return True for Jellyfin stream and download URLs."""
        return parse_stream_url(url) is not None

    async def async_kodi_item(self, context: BackendContext, url: str) -> BackendItem | None:
        """Prefer Kodi's library item, then Jellyfin for Kodi, then the stream."""
        if (stream := parse_stream_url(url)) is None:
            return None
        if (item := await async_get_item(context.session, stream)) is None:
            return None
        if library_item := await async_find_in_kodi_library(context.client, item):
            return BackendItem(item=library_item)  # Kodi knows title and artwork itself
        file = addon_path(item.item_id) if ADDON_JELLYFIN in await context.get_addons() else url
        return BackendItem(item={"file": file}, title=item.title, thumbnail=item.thumbnail)

    async def async_title(
        self, context: BackendContext, file: str
    ) -> tuple[str | None, str | None] | None:
        """Read title and image of a stream entry from Jellyfin."""
        if (stream := parse_stream_url(file)) is None:
            return None
        if (item := await async_get_item(context.session, stream)) is None:
            return None
        return item.title, item.thumbnail
