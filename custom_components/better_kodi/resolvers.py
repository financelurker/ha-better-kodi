"""Turn page URLs into items Kodi can play.

Each resolver looks at a URL and, if it recognizes it, returns the Kodi
playlist item to use. Resolvers that rely on a Kodi add-on are only used when
that add-on is installed and enabled. See docs/DESIGN.md for the table.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from .jellyfin import ADDON_JELLYFIN, addon_path, async_get_item, parse_stream_url

_LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

ADDON_YOUTUBE = "plugin.video.youtube"
ADDON_JELLYCON = "plugin.video.jellycon"
ADDON_SENDTOKODI = "plugin.video.sendtokodi"

KODI_PATH_SCHEMES = frozenset(
    {"plugin", "special", "smb", "nfs", "ftp", "sftp", "upnp", "rtsp", "rtmp", "pvr"}
)
MEDIA_EXTENSIONS = frozenset(
    {
        "3gp", "avi", "flv", "m2ts", "m3u8", "m4v", "mkv", "mov", "mp4", "mpd",
        "mpeg", "mpg", "ogv", "ts", "webm", "wmv",
        "aac", "flac", "m4a", "mp3", "ogg", "opus", "wav",
    }
)  # fmt: skip

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_PATH_ID = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})(?:[/?]|$)")
YOUTUBE_OEMBED = "https://www.youtube.com/oembed"

RUMBLE_HOSTS = frozenset({"rumble.com", "www.rumble.com"})
RUMBLE_EMBED_PATH = re.compile(r"^/embed/(?:[0-9a-z]+\.)?([0-9a-z]+)/?$")
RUMBLE_EMBED_IN_PAGE = re.compile(r"rumble\.com/embed/(?:[0-9a-z]+\.)?([0-9a-z]+)")
RUMBLE_API = "https://rumble.com/embedJS/u3/"

JELLYFIN_ID = re.compile(r"^[0-9a-f]{32}$")


class ResolveError(Exception):
    """A URL was recognized but could not be resolved."""

    def __init__(self, message: str, *, fallback: bool = True) -> None:
        """Store whether SendToKodi may still try the URL."""
        super().__init__(message)
        self.fallback = fallback


@dataclass(slots=True)
class ResolvedMedia:
    """A Kodi playlist item for a URL."""

    item: dict[str, Any]
    resolver: str
    title: str | None = None
    thumbnail: str | None = None
    queueable: bool = True

    @property
    def file(self) -> str | None:
        """Return the Kodi file path of the item, if it has one."""
        return self.item.get("file")


@dataclass(slots=True)
class ResolveContext:
    """What resolvers may use."""

    session: aiohttp.ClientSession
    addons: frozenset[str]


type Resolver = Callable[[str, ResolveContext], Awaitable[ResolvedMedia | None]]


async def async_resolve(url: str, context: ResolveContext) -> ResolvedMedia:
    """Resolve a URL with the first resolver that recognizes it."""
    url = url.strip()
    for resolver in RESOLVERS:
        try:
            if (result := await resolver(url, context)) is not None:
                return result
        except ResolveError as err:
            _LOGGER.debug("%s could not resolve %s: %s", resolver.__name__, url, err)
            if err.fallback and ADDON_SENDTOKODI in context.addons:
                return _sendtokodi(url)
            raise
    return ResolvedMedia(item={"file": url}, resolver="passthrough")


# --- YouTube -----------------------------------------------------------------


def parse_youtube(url: str) -> tuple[str, str] | None:
    """Return ("video", id) or ("playlist", id) for a YouTube URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/")[0]
        return ("video", video_id) if YOUTUBE_ID.match(video_id) else None
    if host not in YOUTUBE_HOSTS:
        return None
    if parts.path == "/watch" and (video_id := query.get("v", [""])[0]):
        return ("video", video_id) if YOUTUBE_ID.match(video_id) else None
    if match := YOUTUBE_PATH_ID.match(parts.path):
        return ("video", match.group(1))
    if parts.path == "/playlist" and (playlist_id := query.get("list", [""])[0]):
        return ("playlist", playlist_id)
    return None


async def resolve_youtube(url: str, context: ResolveContext) -> ResolvedMedia | None:
    """Resolve YouTube videos and playlists for the YouTube add-on."""
    if (parsed := parse_youtube(url)) is None:
        return None
    if ADDON_YOUTUBE not in context.addons:
        raise ResolveError("the YouTube add-on is not installed in Kodi")
    kind, youtube_id = parsed
    if kind == "playlist":
        return ResolvedMedia(
            item={
                "file": f"plugin://{ADDON_YOUTUBE}/play/?"
                + urlencode({"playlist_id": youtube_id, "order": "default", "action": "play"})
            },
            resolver="youtube",
            queueable=False,
        )
    title, thumbnail = await _youtube_oembed(context.session, youtube_id)
    return ResolvedMedia(
        item={"file": f"plugin://{ADDON_YOUTUBE}/play/?video_id={youtube_id}"},
        resolver="youtube",
        title=title,
        thumbnail=thumbnail,
    )


def youtube_page_url(kind: str, youtube_id: str) -> str:
    """Return the YouTube page URL of a video or playlist."""
    if kind == "playlist":
        return f"https://www.youtube.com/playlist?list={youtube_id}"
    return f"https://www.youtube.com/watch?v={youtube_id}"


async def _youtube_oembed(
    session: aiohttp.ClientSession, video_id: str, kind: str = "video"
) -> tuple[str | None, str | None]:
    """Fetch title and thumbnail of a YouTube video or playlist, without an API key."""
    try:
        async with session.get(
            YOUTUBE_OEMBED,
            params={"url": youtube_page_url(kind, video_id), "format": "json"},
            timeout=HTTP_TIMEOUT,
        ) as response:
            if response.status != 200:
                return None, None
            data = await response.json(content_type=None)
    except aiohttp.ClientError, TimeoutError, ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data.get("title"), data.get("thumbnail_url")


# --- Rumble ------------------------------------------------------------------


async def resolve_rumble(url: str, context: ResolveContext) -> ResolvedMedia | None:
    """Resolve Rumble videos to a direct video URL, like Cast Kodi does."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in RUMBLE_HOSTS:
        return None
    if match := RUMBLE_EMBED_PATH.match(parts.path):
        embed_id = match.group(1)
    elif parts.path.startswith("/v") and parts.path.endswith(".html"):
        embed_id = await _rumble_embed_id_from_page(context.session, url)
    else:
        return None
    video = await _rumble_video(context.session, embed_id)
    if (stream := pick_rumble_stream(video)) is None:
        raise ResolveError(f"no playable stream for Rumble video {embed_id}")
    return ResolvedMedia(
        item={"file": stream},
        resolver="rumble",
        title=video.get("title") or None,
        thumbnail=video.get("i") or None,
    )


async def _rumble_embed_id_from_page(session: aiohttp.ClientSession, url: str) -> str:
    """Find the embed ID on a Rumble video page."""
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            response.raise_for_status()
            page = await response.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ResolveError(f"could not load {url}: {err}") from err
    if (match := RUMBLE_EMBED_IN_PAGE.search(page)) is None:
        raise ResolveError(f"no embed ID found on {url}")
    return match.group(1)


async def _rumble_video(session: aiohttp.ClientSession, embed_id: str) -> dict[str, Any]:
    """Load the video description from Rumble's embed API."""
    try:
        async with session.get(
            RUMBLE_API,
            params={"request": "video", "ver": 2, "v": embed_id},
            timeout=HTTP_TIMEOUT,
        ) as response:
            response.raise_for_status()
            video = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise ResolveError(f"Rumble API failed for {embed_id}: {err}") from err
    if not isinstance(video, dict):
        raise ResolveError(f"Rumble video {embed_id} not found")
    return video


def pick_rumble_stream(video: dict[str, Any]) -> str | None:
    """Pick the best stream: HLS for live streams, else the highest MP4."""
    streams: dict[str, list[tuple[int, str]]] = {}
    for kind, entries in (video.get("ua") or {}).items():
        if isinstance(entries, dict):
            pairs = list(entries.items())
        elif isinstance(entries, list):
            pairs = [(None, entry) for entry in entries]
        else:
            continue
        for height_key, entry in pairs:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            meta = entry.get("meta") or {}
            height = _as_int(meta.get("h")) or _as_int(height_key) or 0
            streams.setdefault(kind, []).append((height, entry["url"]))

    order = ("hls", "mp4", "webm") if video.get("live") == 2 else ("mp4", "hls", "webm")
    for kind in order:
        if candidates := streams.get(kind):
            return max(candidates)[1]
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


# --- Jellyfin ----------------------------------------------------------------


def parse_jellyfin_item_id(url: str) -> str | None:
    """Return the item ID of a Jellyfin web link (…#/details?id=…)."""
    fragment = urlsplit(url).fragment.lstrip("!")
    if not fragment:
        return None
    route = urlsplit(fragment)
    if route.path.rstrip("/") not in ("/details", "/item", "details", "item"):
        return None
    item_id = parse_qs(route.query).get("id", [""])[0].lower()
    return item_id if JELLYFIN_ID.match(item_id) else None


async def resolve_jellyfin(url: str, context: ResolveContext) -> ResolvedMedia | None:
    """Resolve Jellyfin web links for Jellyfin for Kodi or JellyCon."""
    if (item_id := parse_jellyfin_item_id(url)) is None:
        return None
    if ADDON_JELLYFIN in context.addons:
        return ResolvedMedia(item={"file": addon_path(item_id)}, resolver="jellyfin")
    if ADDON_JELLYCON in context.addons:
        return ResolvedMedia(
            item={
                "file": f"plugin://{ADDON_JELLYCON}/?"
                + urlencode({"mode": "PLAY", "item_id": item_id})
            },
            resolver="jellycon",
            queueable=False,
        )
    # SendToKodi cannot help: Jellyfin needs a login that only the add-ons have.
    raise ResolveError(
        "Jellyfin links need the Jellyfin for Kodi or JellyCon add-on in Kodi", fallback=False
    )


# --- Direct paths and fallback -------------------------------------------------


async def resolve_direct(url: str, context: ResolveContext) -> ResolvedMedia | None:
    """Pass Kodi paths and direct media files through unchanged."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in KODI_PATH_SCHEMES:
        return ResolvedMedia(item={"file": url}, resolver="direct")
    if scheme in ("http", "https"):
        extension = parts.path.rsplit(".", 1)[-1].lower() if "." in parts.path else ""
        if extension in MEDIA_EXTENSIONS:
            return ResolvedMedia(item={"file": url}, resolver="direct")
    return None


async def resolve_sendtokodi(url: str, context: ResolveContext) -> ResolvedMedia | None:
    """Hand any other web page to SendToKodi, which uses yt-dlp."""
    if ADDON_SENDTOKODI not in context.addons:
        return None
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return None
    return _sendtokodi(url)


def _sendtokodi(url: str) -> ResolvedMedia:
    return ResolvedMedia(
        # SendToKodi takes the page URL unencoded after "?", as Cast Kodi sends it.
        item={"file": f"plugin://{ADDON_SENDTOKODI}/?{url}"},
        resolver="sendtokodi",
    )


RESOLVERS: tuple[Resolver, ...] = (
    resolve_youtube,
    resolve_rumble,
    resolve_jellyfin,
    resolve_direct,
    resolve_sendtokodi,
)


# --- Titles for queue entries --------------------------------------------------
#
# Kodi labels an add-on path that has not been played yet with a part of the
# path, for example "play" for plugin://plugin.video.youtube/play/?video_id=…
# Entries added by other apps (Cast Kodi, Yatse, …) or before a restart are
# therefore named here from their path.

KODI_MARKUP = re.compile(r"\[/?(?:B|I|LIGHT|UPPERCASE|LOWERCASE|CAPITALIZE|COLOR[^\]]*)\]")


def strip_kodi_markup(text: str) -> str:
    """Remove Kodi label formatting such as [B]…[/B] or [COLOR red]…[/COLOR]."""
    return " ".join(KODI_MARKUP.sub("", text.replace("[CR]", " ")).split())


def youtube_from_kodi_path(file: str) -> tuple[str, str] | None:
    """Return ("video" | "playlist", id) for a YouTube item in Kodi's queue."""
    parts = urlsplit(file)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    if scheme == "plugin" and host == ADDON_YOUTUBE:
        if video_id := query.get("video_id", query.get("videoid", [""]))[0]:
            return ("video", video_id) if YOUTUBE_ID.match(video_id) else None
        if playlist_id := query.get("playlist_id", query.get("playlist", [""]))[0]:
            return ("playlist", playlist_id)
        if uri := query.get("uri", [""])[0]:
            return parse_youtube(uri)
        return None
    if scheme == "plugin" and host == ADDON_SENDTOKODI:
        # SendToKodi gets the page URL unencoded after "?".
        return parse_youtube(file.split("?", 1)[1]) if "?" in file else None
    if scheme in ("http", "https"):
        return parse_youtube(file)
    return None


async def async_lookup_title(
    session: aiohttp.ClientSession, file: str
) -> tuple[str | None, str | None] | None:
    """Return title and thumbnail for a queue entry, or None if unknown."""
    if (stream := parse_stream_url(file)) is not None:
        if (item := await async_get_item(session, stream)) is None:
            return None
        return item.title, item.thumbnail
    if (youtube := youtube_from_kodi_path(file)) is None:
        return None
    kind, youtube_id = youtube
    title, thumbnail = await _youtube_oembed(session, youtube_id, kind)
    if title is None and thumbnail is None:
        return None
    return title, thumbnail
