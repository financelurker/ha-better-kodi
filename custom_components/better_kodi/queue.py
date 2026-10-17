"""Queue handling on Kodi's video playlist.

This module only talks to Kodi through KodiClient and knows nothing about
Home Assistant, so the queue rules can be tested on their own.
"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .const import VIDEO_PLAYLIST
from .kodi_client import KodiClient, KodiRpcError

ITEM_PROPERTIES = ["title", "file", "thumbnail", "duration", "showtitle", "season", "episode"]
START_TIMEOUT = 15.0
START_POLL_INTERVAL = 0.5


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

    @property
    def playing(self) -> bool:
        """Return True if a video player is active."""
        return self.player_id is not None

    @property
    def in_queue(self) -> bool:
        """Return True if the active video comes from the queue."""
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
    """Titles and thumbnails Better Kodi knows for add-on paths."""

    titles: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

    def remember(self, file: str | None, title: str | None, thumbnail: str | None) -> None:
        """Remember what a file is called, if we know."""
        if file and (title or thumbnail):
            self.titles[file] = (title, thumbnail)
            if len(self.titles) > 500:
                del self.titles[next(iter(self.titles))]

    def lookup(self, file: str | None) -> tuple[str | None, str | None]:
        """Return the remembered title and thumbnail of a file."""
        return self.titles.get(file or "", (None, None))


class KodiQueue:
    """Read and change Kodi's video queue."""

    def __init__(self, client: KodiClient) -> None:
        """Initialize with a Kodi client."""
        self.client = client
        self.known = KnownMedia()

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
            return QueueStatus(None, None, length)
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
        items = []
        for index, raw in enumerate((result or {}).get("items") or []):
            file = raw.get("file") or None
            known_title, known_thumbnail = self.known.lookup(file)
            title = raw.get("title") or raw.get("label") or ""
            if not title or title == file:
                title = known_title or title or file or f"#{index + 1}"
            if raw.get("showtitle") and raw.get("episode", -1) >= 0:
                title = f"{raw['showtitle']} – {title}"
            items.append(
                QueueItem(
                    position=index,
                    title=title,
                    file=file,
                    media_type=raw.get("type") or "unknown",
                    thumbnail=raw.get("thumbnail") or known_thumbnail or None,
                    duration=raw.get("duration") or None,
                    current=status.in_queue and status.position == index,
                )
            )
        return items

    async def enqueue(self, item: dict[str, Any], mode: EnqueueMode) -> None:
        """Play or queue an item according to the rules in docs/DESIGN.md."""
        status = await self.status()
        if mode is EnqueueMode.REPLACE or not status.playing:
            await self.replace(item)
            return
        if status.in_queue:
            assert status.position is not None
            after_current = status.position + 1
            if mode is EnqueueMode.ADD:
                await self._add(item)
                return
            await self.client.call(
                "Playlist.Insert",
                {"playlistid": VIDEO_PLAYLIST, "position": after_current, "item": item},
            )
            if mode is EnqueueMode.PLAY:
                await self.client.call(
                    "Player.GoTo", {"playerid": status.player_id, "to": after_current}
                )
            return
        # Kodi plays something that was not started from the queue.
        await self._add(item)
        if mode is EnqueueMode.PLAY:
            await self.play_position(status.length)

    async def replace(self, item: dict[str, Any] | list[dict[str, Any]]) -> None:
        """Clear the queue, put the item(s) in it and start playing."""
        await self.clear()
        await self._add(item)
        await self.play_position(0)

    async def play_position(self, position: int) -> None:
        """Start playing the queue at a position."""
        await self.client.call(
            "Player.Open", {"item": {"playlistid": VIDEO_PLAYLIST, "position": position}}
        )

    async def clear(self) -> None:
        """Clear the video queue."""
        await self.client.call("Playlist.Clear", {"playlistid": VIDEO_PLAYLIST})

    async def remove(self, position: int) -> None:
        """Remove an item from the queue."""
        status = await self.status()
        self._check_position(position, status.length)
        if status.in_queue and status.position == position:
            raise QueueError("The item that is playing cannot be removed")
        await self.client.call(
            "Playlist.Remove", {"playlistid": VIDEO_PLAYLIST, "position": position}
        )

    async def move(self, position: int, to_position: int) -> None:
        """Move an item to another position by swapping neighbours."""
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

    async def snapshot(self) -> HandoverSnapshot:
        """Capture what is playing, for a handover."""
        status = await self.status()
        if not status.playing:
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

    async def restore(self, snapshot: HandoverSnapshot, *, resume: bool = True) -> None:
        """Load a snapshot and continue where it was."""
        await self.clear()
        await self._add(snapshot.items)
        await self.play_position(snapshot.position)
        if not resume or not _has_time(snapshot.time):
            return
        player_id = await self._wait_for_player()
        try:
            await self.client.call(
                "Player.Seek", {"playerid": player_id, "value": {"time": snapshot.time}}
            )
        except KodiRpcError as err:
            raise QueueError(f"Playback started, but seeking failed: {err.message}") from err

    async def _add(self, item: dict[str, Any] | list[dict[str, Any]]) -> None:
        await self.client.call("Playlist.Add", {"playlistid": VIDEO_PLAYLIST, "item": item})

    async def _wait_for_player(self) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT
        while True:
            if (player_id := await self.video_player_id()) is not None:
                return player_id
            if loop.time() >= deadline:
                raise QueueError("Kodi did not start playing in time")
            await asyncio.sleep(START_POLL_INTERVAL)

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
    key = {"movie": "movieid", "episode": "episodeid", "musicvideo": "musicvideoid"}.get(
        raw.get("type", "")
    )
    if key and raw.get("id") is not None:
        return {key: raw["id"]}
    return None


def _has_time(time: dict[str, int] | None) -> bool:
    return bool(time) and any(time.get(part, 0) for part in ("hours", "minutes", "seconds"))
