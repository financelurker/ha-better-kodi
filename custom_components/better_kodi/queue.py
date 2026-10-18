"""Queue handling on Kodi's video playlist.

This module only talks to Kodi through KodiClient and knows nothing about
Home Assistant, so the queue rules can be tested on their own.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import time
from typing import Any
from urllib.parse import unquote

from .const import LIBRARY_ID_KEYS, VIDEO_PLAYLIST
from .kodi_client import KodiClient, KodiRpcError
from .resolvers import strip_kodi_markup, youtube_thumbnail

_LOGGER = logging.getLogger(__name__)

ITEM_PROPERTIES = ["title", "file", "thumbnail", "duration", "showtitle", "season", "episode"]

# Kodi answers Player.Open at once and starts the player a little later; add-on
# items can take several seconds. During this window the queue counts as playing.
START_WINDOW = 20.0
SEEK_TIMEOUT = 15.0
POLL_INTERVAL = 0.5
TITLE_LOOKUP_TIMEOUT = 8.0
LOOKUP_RETRY_AFTER = 600.0
MAX_PARALLEL_LOOKUPS = 4
MAX_LOOKUPS_PER_READ = 25

type TitleLookup = Callable[[str], Awaitable[tuple[str | None, str | None] | None]]


class EnqueueMode(StrEnum):
    """How a new item interacts with the queue (same values as Home Assistant)."""

    PLAY = "play"
    NEXT = "next"
    ADD = "add"
    REPLACE = "replace"


class QueueError(Exception):
    """A queue operation is not possible."""


@dataclass(slots=True, frozen=True)
class QueueStatus:
    """Where Kodi's video player stands in the queue."""

    player_id: int | None
    position: int | None
    length: int
    time: dict[str, int] | None = None
    starting: bool = False

    @property
    def playing(self) -> bool:
        """Return True if a video plays, or the queue was just started."""
        return self.player_id is not None or self.starting

    @property
    def in_queue(self) -> bool:
        """Return True if the current video comes from the queue."""
        return self.playing and self.position is not None and self.position >= 0


@dataclass(slots=True)
class QueueItem:
    """One entry of the queue, ready for the media browser and services."""

    position: int
    title: str
    file: str | None
    media_type: str
    thumbnail: str | None = None
    duration: int | None = None
    current: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the item as a plain dict for service responses."""
        return {
            "position": self.position,
            "title": self.title,
            "file": self.file,
            "type": self.media_type,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "current": self.current,
        }


@dataclass(slots=True)
class HandoverSnapshot:
    """What is needed to continue playback on another Kodi."""

    items: list[dict[str, Any]]
    position: int
    time: dict[str, int] | None


@dataclass(slots=True)
class KnownMedia:
    """Titles and thumbnails Better Kodi knows for add-on paths and streams.

    Kodi labels such items with a part of their path (for example "play" for
    a YouTube add-on path) until they are played, so these are preferred.
    """

    titles: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    # file -> monotonic time of a lookup that found nothing; retried after a while,
    # because the service may only have been unreachable.
    unknown: dict[str, float] = field(default_factory=dict)
    limit: int = 2000

    def remember(self, file: str | None, title: str | None, thumbnail: str | None) -> None:
        """Remember what a file is called."""
        if file and (title or thumbnail):
            self.titles[file] = (title, thumbnail)
            while len(self.titles) > self.limit:
                del self.titles[next(iter(self.titles))]

    def lookup(self, file: str | None) -> tuple[str | None, str | None]:
        """Return the remembered title and thumbnail of a file."""
        return self.titles.get(file or "", (None, None))

    def knows(self, file: str) -> bool:
        """Return True if the file needs no lookup now."""
        if file in self.titles:
            return True
        failed_at = self.unknown.get(file)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at > LOOKUP_RETRY_AFTER:
            del self.unknown[file]
            return False
        return True

    def lookup_failed(self, file: str) -> None:
        """Remember that a lookup found nothing; it is tried again later."""
        self.unknown[file] = time.monotonic()
        while len(self.unknown) > self.limit:
            del self.unknown[next(iter(self.unknown))]


class KodiQueue:
    """Read and change Kodi's video queue.

    Changing operations are serialized with a lock, because Home Assistant may
    run several service calls at the same time.
    """

    def __init__(self, client: KodiClient, title_lookup: TitleLookup | None = None) -> None:
        """Initialize with a Kodi client and an optional way to look up titles."""
        self.client = client
        self.known = KnownMedia()
        self._title_lookup = title_lookup
        self._lookups_running: dict[str, asyncio.Task[None]] = {}
        self._lookup_slots = asyncio.Semaphore(MAX_PARALLEL_LOOKUPS)
        self._lock = asyncio.Lock()
        self._started: tuple[float, int] | None = None  # (monotonic time, position)

    # --- Reading ----------------------------------------------------------------

    async def video_player_id(self) -> int | None:
        """Return the ID of the active video player, if any."""
        players = await self.client.call("Player.GetActivePlayers") or []
        for player in players:
            if player.get("type") == "video":
                return int(player["playerid"])
        return None

    async def status(self) -> QueueStatus:
        """Return the current queue status."""
        size = await self.client.call(
            "Playlist.GetProperties",
            {"playlistid": VIDEO_PLAYLIST, "properties": ["size"]},
        )
        length = int((size or {}).get("size", 0))
        if (player_id := await self.video_player_id()) is None:
            if (start := self._start_pending()) is not None and length:
                return QueueStatus(None, start, length, starting=True)
            return QueueStatus(None, None, length)
        self._started = None
        props = await self.client.call(
            "Player.GetProperties",
            {"playerid": player_id, "properties": ["playlistid", "position", "time"]},
        )
        position = None
        if props.get("playlistid") == VIDEO_PLAYLIST and props.get("position", -1) >= 0:
            position = int(props["position"])
        return QueueStatus(player_id, position, length, props.get("time"))

    async def items(self) -> list[QueueItem]:
        """Return the queue items, with the current item marked."""
        result = await self.client.call(
            "Playlist.GetItems",
            {"playlistid": VIDEO_PLAYLIST, "properties": ITEM_PROPERTIES},
        )
        status = await self.status()
        raw_items = (result or {}).get("items") or []
        await self._look_up_titles(raw_items)
        items = []
        for index, raw in enumerate(raw_items):
            file = raw.get("file") or None
            known_title, known_thumbnail = self.known.lookup(file)
            kodi_title = strip_kodi_markup(raw.get("title") or raw.get("label") or "")
            if file and _needs_title(raw):
                title = known_title or kodi_title or file or f"#{index + 1}"
            else:
                title = kodi_title or known_title or file or f"#{index + 1}"
            if raw.get("showtitle") and raw.get("episode", -1) >= 0:
                title = f"{raw['showtitle']} – {title}"
            items.append(
                QueueItem(
                    position=index,
                    title=title,
                    file=file,
                    media_type=raw.get("type") or "unknown",
                    thumbnail=known_thumbnail
                    or _kodi_image(raw.get("thumbnail"))
                    or youtube_thumbnail(file),
                    duration=raw.get("duration") or None,
                    current=status.in_queue and status.position == index,
                )
            )
        return items

    async def _look_up_titles(self, raw_items: list[dict[str, Any]]) -> None:
        """Name entries whose title Kodi doesn't know yet (add-on paths)."""
        if self._title_lookup is None:
            return
        lookup = self._title_lookup
        files = list(
            dict.fromkeys(
                file
                for raw in raw_items
                if (file := raw.get("file")) and _needs_title(raw) and not self.known.knows(file)
            )
        )[:MAX_LOOKUPS_PER_READ]
        if not files:
            return

        async def look_up(file: str) -> None:
            async with self._lookup_slots:
                try:
                    async with asyncio.timeout(TITLE_LOOKUP_TIMEOUT):
                        found = await lookup(file)
                except TimeoutError:
                    return  # try again next time
                except Exception:  # a broken answer must not break the queue
                    _LOGGER.debug("Title lookup for %s failed", file, exc_info=True)
                    found = None
            if found is None:
                self.known.lookup_failed(file)
            else:
                self.known.remember(file, *found)

        tasks = []
        for file in files:
            if (task := self._lookups_running.get(file)) is None:
                task = asyncio.ensure_future(look_up(file))
                self._lookups_running[file] = task
                task.add_done_callback(lambda _, file=file: self._lookups_running.pop(file, None))
            tasks.append(task)
        await asyncio.gather(*tasks)

    # --- Changing -----------------------------------------------------------------

    async def enqueue(self, item: dict[str, Any], mode: EnqueueMode) -> None:
        """Play or queue an item according to the rules in docs/DESIGN.md."""
        async with self._lock:
            status = await self.status()
            if mode is EnqueueMode.REPLACE or not status.playing:
                await self._replace(item)
                return
            if not status.in_queue:
                # Kodi plays something that was not started from the queue.
                await self._add(item)
                if mode is EnqueueMode.PLAY:
                    await self._open(status.length)
                return
            assert status.position is not None
            after_current = status.position + 1
            if mode is EnqueueMode.ADD:
                await self._add(item)
                return
            await self.client.call(
                "Playlist.Insert",
                {"playlistid": VIDEO_PLAYLIST, "position": after_current, "item": item},
            )
            if mode is EnqueueMode.NEXT:
                return
            if status.player_id is None:
                await self._open(after_current)
            else:
                await self.client.call(
                    "Player.GoTo", {"playerid": status.player_id, "to": after_current}
                )

    async def replace(self, item: dict[str, Any] | list[dict[str, Any]]) -> None:
        """Clear the queue, put the item(s) in it and start playing."""
        async with self._lock:
            await self._replace(item)

    async def play_position(self, position: int) -> None:
        """Start playing the queue at a position."""
        async with self._lock:
            await self._open(position)

    async def clear(self) -> None:
        """Clear the video queue."""
        async with self._lock:
            await self._clear()

    async def remove(self, position: int) -> None:
        """Remove an item from the queue."""
        async with self._lock:
            status = await self.status()
            self._check_position(position, status.length)
            if status.in_queue and status.position == position:
                raise QueueError("The item that is playing cannot be removed")
            await self.client.call(
                "Playlist.Remove", {"playlistid": VIDEO_PLAYLIST, "position": position}
            )

    async def move(self, position: int, to_position: int) -> None:
        """Move an item to another position by swapping neighbours."""
        async with self._lock:
            status = await self.status()
            self._check_position(position, status.length)
            self._check_position(to_position, status.length)
            step = 1 if to_position > position else -1
            for current in range(position, to_position, step):
                await self.client.call(
                    "Playlist.Swap",
                    {
                        "playlistid": VIDEO_PLAYLIST,
                        "position1": current,
                        "position2": current + step,
                    },
                )

    # --- Handover -----------------------------------------------------------------

    async def snapshot(self) -> HandoverSnapshot:
        """Capture what is playing, for a handover."""
        status = await self.status()
        if status.player_id is None:
            raise QueueError("Nothing is playing on this Kodi")
        if status.in_queue:
            assert status.position is not None
            result = await self.client.call(
                "Playlist.GetItems",
                {"playlistid": VIDEO_PLAYLIST, "properties": ["file"]},
            )
            raw_items = (result or {}).get("items") or []
            position = status.position
        else:
            result = await self.client.call(
                "Player.GetItem",
                {"playerid": status.player_id, "properties": ["file"]},
            )
            raw_items = [(result or {}).get("item") or {}]
            position = 0
        items = [portable_item(raw) for raw in raw_items]
        if any(item is None for item in items):
            raise QueueError("An item in the queue has no file path and cannot be handed over")
        return HandoverSnapshot(
            items=[item for item in items if item is not None],
            position=position,
            time=status.time,
        )

    async def restore(self, snapshot: HandoverSnapshot, *, resume: bool = True) -> bool:
        """Load a snapshot and continue there.

        Returns False if playback started but could not be moved to the same
        time; the caller should finish the handover anyway.
        """
        async with self._lock:
            await self._clear()
            await self._add(snapshot.items)
            await self._open(snapshot.position)
        if not resume or not _has_time(snapshot.time):
            return True
        return await self._seek_when_ready(snapshot.time or {})

    # --- Internals ----------------------------------------------------------------

    async def _replace(self, item: dict[str, Any] | list[dict[str, Any]]) -> None:
        await self._clear()
        await self._add(item)
        await self._open(0)

    async def _clear(self) -> None:
        await self.client.call("Playlist.Clear", {"playlistid": VIDEO_PLAYLIST})
        self._started = None

    async def _add(self, item: dict[str, Any] | list[dict[str, Any]]) -> None:
        await self.client.call("Playlist.Add", {"playlistid": VIDEO_PLAYLIST, "item": item})

    async def _open(self, position: int) -> None:
        await self.client.call(
            "Player.Open", {"item": {"playlistid": VIDEO_PLAYLIST, "position": position}}
        )
        self._started = (time.monotonic(), position)

    def _start_pending(self) -> int | None:
        if self._started is None:
            return None
        started_at, position = self._started
        if time.monotonic() - started_at > START_WINDOW:
            self._started = None
            return None
        return position

    async def _seek_when_ready(self, target: dict[str, int]) -> bool:
        """Seek as soon as Kodi accepts it; a fresh player may refuse at first."""
        deadline = time.monotonic() + SEEK_TIMEOUT
        while True:
            player_id = await self.video_player_id()
            if player_id is not None:
                try:
                    await self.client.call(
                        "Player.Seek", {"playerid": player_id, "value": {"time": target}}
                    )
                except KodiRpcError as err:
                    _LOGGER.debug("Seek not possible yet: %s", err)
                else:
                    return True
            if time.monotonic() >= deadline:
                _LOGGER.warning("Playback started, but Kodi did not accept seeking in time")
                return False
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    def _check_position(position: int, length: int) -> None:
        if not 0 <= position < length:
            raise QueueError(f"Position {position} is outside the queue (0–{length - 1})")


def portable_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return a playlist item that also works on another Kodi.

    File paths are preferred, because library IDs differ between Kodi
    instances unless they share a database.
    """
    if file := raw.get("file"):
        return {"file": file}
    key = LIBRARY_ID_KEYS.get(raw.get("type", ""))
    if key and raw.get("id") is not None:
        return {key: raw["id"]}
    return None


def _kodi_image(image: str | None) -> str | None:
    """Return Kodi's image of an entry, or None for none or a skin placeholder.

    Kodi always wraps images as image://<encoded value>/. They are loaded
    through Home Assistant's image proxy, which asks Kodi; that also works for
    images only Kodi can reach. A skin placeholder such as
    image://DefaultVideo.png/ has nothing useful to show.
    """
    if not image:
        return None
    if image.startswith("image://"):
        inner = unquote(image.removeprefix("image://").split("/", 1)[0])
        if "://" not in inner and "/" not in inner and "\\" not in inner:
            return None  # a skin placeholder, not a file
    return image


def _needs_title(raw: dict[str, Any]) -> bool:
    """Return True for entries that Kodi only knows by their path.

    Kodi leaves "title" empty for paths that haven't been played (their label is
    a part of the path). Entries an add-on filled in have a title already.
    """
    if raw.get("type") not in (None, "", "unknown"):
        return False  # library items have real titles
    return raw.get("title", "") in ("", raw["file"])


def _has_time(value: dict[str, int] | None) -> bool:
    return bool(value) and any(value.get(part, 0) for part in ("hours", "minutes", "seconds"))
