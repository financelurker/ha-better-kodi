"""Announcements on Kodi: interrupt, play a clip, continue where Kodi was.

Kodi can't mix a clip into a running video, so Better Kodi notes what plays,
plays the clip instead and then opens the noted item again at the same time,
with the same audio track, subtitle, pause state and volume. See
docs/DESIGN.md ("Announcements") for the rules.

Like queue.py, this module only talks to Kodi through KodiClient.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import re
import time
from typing import Any
from urllib.parse import unquote

from .const import LIBRARY_ID_KEYS
from .kodi_client import KodiClient, KodiError, KodiRpcError
from .redact import redact_url

_LOGGER = logging.getLogger(__name__)

MUSIC_PLAYLIST = 0
VIDEO_PLAYLIST = 1
PLAYLISTS = (MUSIC_PLAYLIST, VIDEO_PLAYLIST)
POLL_INTERVAL = 0.25
IDLE_CHECK_INTERVAL = 1.0
# Without notifications, the end of a clip is judged from Kodi's time, which
# is a poll old: a clip that stopped this close to its end ended by itself.
END_TOLERANCE = 2.5
# With notifications, how long to wait for Kodi's "stopped" after the player is gone
STOP_NOTIFICATION_WAIT = 2.0
# Without notifications, a clip in the music playlist with no player for this
# long has played already (shorter than a poll).
UNSEEN_CLIP_GRACE = 3.0
SEEK_TOLERANCE = 3.0
SEEK_TIMEOUT = 15.0
STREAMS_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0
# How often to look whether a reopened item that was added to its playlist
# (see _keep_queue_after) still plays, when no notification says so
TAIL_CHECK_INTERVAL = 10.0
TAIL_CHECK_TIMEOUT = 3.0
# Kodi shows a player's playlist length only while that playlist is Kodi's current one
PLAYLIST_LENGTH_LABEL = {
    "video": "VideoPlayer.PlaylistLength",
    "audio": "MusicPlayer.PlaylistLength",
}
DIALOG_TIME_TOLERANCE = 3
TIME_IN_TEXT = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
STREAM_PROPERTIES = [
    "currentaudiostream",
    "audiostreams",
    "currentsubtitle",
    "subtitleenabled",
    "subtitles",
]

type Clock = Callable[[], float]
type TaskFactory = Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]]


class Outcome(StrEnum):
    """How an announcement ended (all without error)."""

    DONE = "done"  # played and Kodi continued where it was (or had nothing to continue)
    MUTED = "muted"  # Kodi is muted: nothing was interrupted
    OVERRIDE = "override"  # someone used Kodi meanwhile: Better Kodi left it alone
    PASSED_ON = "passed_on"  # played; the next announcement continues afterwards


class AnnounceError(Exception):
    """An announcement failed."""


class ClipError(AnnounceError):
    """Kodi did not play the clip."""


class RestoreError(AnnounceError):
    """Kodi could not continue what it played before the announcement."""


@dataclass(slots=True, frozen=True)
class Stream:
    """An audio track or subtitle, as Kodi lists it."""

    index: int
    language: str
    name: str

    @classmethod
    def from_kodi(cls, raw: Any) -> Stream | None:
        """Read Kodi's stream description; None for "no stream"."""
        if not isinstance(raw, dict) or raw.get("index") is None:
            return None
        if not raw.get("language") and not raw.get("name") and not raw.get("index"):
            return None  # Kodi answers an empty object when there is none
        return cls(int(raw["index"]), raw.get("language") or "", raw.get("name") or "")

    def find_in(self, raw_streams: Any) -> int | None:
        """Return the index of the same stream among Kodi's streams.

        Indexes can differ after a stream is opened again (for example a new
        stream URL), so language and name decide; the index is the fallback.
        """
        streams = [s for raw in raw_streams or [] if (s := Stream.from_kodi(raw)) is not None]
        same = [s for s in streams if (s.language, s.name) == (self.language, self.name)]
        if any(stream.index == self.index for stream in same):
            return self.index  # several alike (two unnamed English subtitles): the noted one
        if same:
            return same[0].index
        if self.language:
            same_language = [s for s in streams if s.language == self.language]
            if len(same_language) == 1:
                return same_language[0].index
        if any(stream.index == self.index for stream in streams):
            return self.index
        return None


@dataclass(slots=True)
class Playback:
    """What Kodi played before an announcement."""

    kind: str  # "playlist", "item" or "channel"
    file: str | None
    playlist_id: int | None = None
    position: int | None = None
    item: dict[str, Any] | None = None  # for "item" and "channel"
    time: dict[str, int] | None = None
    paused: bool = False
    audio: Stream | None = None
    subtitle: Stream | None = None
    subtitle_enabled: bool | None = None
    player_type: str = "video"
    shuffled: bool = False
    # For an item outside the playlist: whether its playlist is still Kodi's
    # current one, so Kodi goes on with it after the item (otherwise it stops)
    playlist_current: bool = False

    @property
    def seconds(self) -> int:
        """Return the noted time in whole seconds."""
        return int(_seconds(self.time))


@dataclass(slots=True)
class Snapshot:
    """Kodi before an announcement, and what the announcements changed."""

    volume: int | None
    playback: Playback | None
    # Saved playlists (portable items). Kodi replaces the playlist of an item
    # it is asked to open on its own with that item; Better Kodi puts them back.
    playlists: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    volume_set: int | None = None  # what Better Kodi set for the clip
    clip: str | None = None  # the last clip Better Kodi opened
    music_repeat: str | None = None  # the music playlist's repeat, switched off for the clip
    put_back: set[int] = field(default_factory=set)  # playlists Better Kodi put back
    start_timeout: float = 30


@dataclass(slots=True, frozen=True)
class _Tail:
    """A reopened item Better Kodi added after the playlist, to remove later."""

    playlist_id: int
    before: list[dict[str, Any]]  # the entries in front of it
    item: list[dict[str, Any] | None]  # as opened, and as Kodi shows it


@dataclass(slots=True)
class _Listening:
    """Notifications Kodi sent while Better Kodi waited for something."""

    started: bool = False
    stopped: bool = False
    ended: bool | None = None  # Kodi's "end" flag: True if played to the end
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class Announcer:
    """Play announcements on one Kodi, one at a time.

    Commands from Home Assistant (see command()) and announcements don't
    overlap: commands wait for announcements, and an announcement waits for
    running commands before it notes what Kodi plays.
    """

    def __init__(
        self,
        client: KodiClient,
        *,
        clock: Clock = time.monotonic,
        create_task: TaskFactory | None = None,
    ) -> None:
        """Initialize with a Kodi client; create_task starts background tasks."""
        self.client = client
        self._clock = clock
        self._create_task: TaskFactory = create_task or (
            lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name)
        )
        self._lock = asyncio.Lock()
        self._waiting = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._commands = 0
        self._no_commands = asyncio.Event()
        self._no_commands.set()
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._session: Snapshot | None = None
        self._finishing: asyncio.Task[Outcome] | None = None
        self._unawaited: set[asyncio.Task[Outcome]] = set()  # callers cancelled meanwhile
        self._listening: _Listening | None = None
        self._tail_tasks: set[asyncio.Task[Any]] = set()
        self._tail_checks: set[asyncio.Future[bool]] = set()
        self._tail_changed = asyncio.Event()

    def close(self) -> None:
        """Stop watching a reopened item (the entry is removed)."""
        for task in self._tail_tasks:
            task.cancel()
        self._tail_tasks.clear()

    @property
    def queued(self) -> bool:
        """Return True while an announcement waits for its turn."""
        return self._waiting > 0

    @property
    def busy(self) -> bool:
        """Return True while an announcement runs, waits or Kodi continues after one."""
        return not self._idle.is_set()

    @property
    def watching_reopened_item(self) -> bool:
        """Return True while an item reopened after an announcement waits to be removed.

        The entries in front of it are the noted queue, not played entries.
        """
        return bool(self._tail_tasks)

    async def wait_idle(self) -> None:
        """Wait until no announcement runs or waits."""
        await self._idle.wait()

    @contextlib.asynccontextmanager
    async def command(self) -> AsyncIterator[None]:
        """Run a command after the announcements; announcements wait for it.

        Commands the command runs itself (in the same task, such as a handover
        stopping playback) don't wait again.
        """
        task = asyncio.current_task()
        if task in self._command_tasks:
            yield
            return
        while not self._idle.is_set():  # another announcement may start right after one
            await self._idle.wait()
        self._commands += 1
        self._no_commands.clear()
        if task is not None:
            self._command_tasks.add(task)
        try:
            yield
        finally:
            self._command_tasks.discard(task)  # type: ignore[arg-type]
            self._commands -= 1
            if not self._commands:
                self._no_commands.set()

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Have Kodi to oneself, like an announcement (commands wait meanwhile)."""
        async with self._turn():
            yield

    @property
    def _notifications(self) -> bool:
        """Return True if Kodi's notifications arrive (WebSocket connected)."""
        return bool(getattr(self.client, "notifications_connected", False))

    def on_notification(self, method: str, data: dict[str, Any]) -> None:
        """Take Kodi's notifications (over WebSocket), to react without polling delay."""
        if method in ("Player.OnStop", "Player.OnPlay"):
            self._tail_changed.set()
        listening = self._listening
        if listening is None:
            return
        if method in ("Player.OnPlay", "Player.OnAVStart"):
            listening.started = True
        elif method == "Player.OnStop" and listening.started:
            # Only the stop after the clip started counts: the stop of the
            # interrupted video comes before it.
            listening.stopped = True
            end = data.get("end")
            listening.ended = end if isinstance(end, bool) else None
        listening.changed.set()

    async def announce(
        self,
        url: str,
        *,
        volume: int | None = None,
        max_duration: float = 60,
        start_timeout: float = 30,
    ) -> Outcome:
        """Play a clip as an announcement and continue afterwards.

        Announcements that arrive meanwhile play one after another; Kodi
        continues after the last one. If an announcement is cancelled, Kodi
        still continues (in the background).
        """
        async with self._turn():
            return await self._announce(url, volume, max_duration, start_timeout)

    @contextlib.asynccontextmanager
    async def _turn(self) -> AsyncIterator[None]:
        """Wait for the turn: earlier announcements, Kodi continuing, running commands."""
        self._waiting += 1
        self._idle.clear()
        acquired = False
        try:
            async with self._lock:
                acquired = True
                self._waiting -= 1
                for check in list(self._tail_checks):
                    check.cancel()  # tidying up the playlist can wait
                if (finishing := self._finishing) is not None and not finishing.done():
                    with contextlib.suppress(Exception):
                        await asyncio.shield(finishing)
                await self._no_commands.wait()
                yield
        finally:
            if not acquired:
                self._waiting -= 1
            if (
                self._session is not None
                and not self._waiting
                and not self._lock.locked()
                and not self._is_finishing
            ):
                # Cancelled or failed while Kodi was interrupted, or the next
                # announcement this one passed on to was cancelled.
                self._start_finish(orphaned=True)
            self._update_idle()

    @property
    def _is_finishing(self) -> bool:
        # A finished task counts as over even before its done callback ran.
        return self._finishing is not None and not self._finishing.done()

    def _update_idle(self) -> None:
        if self._waiting == 0 and not self._lock.locked() and not self._is_finishing:
            self._idle.set()

    def _start_finish(self, *, orphaned: bool = False) -> asyncio.Task[Outcome]:
        """Continue playback in a task of its own, so a cancelled caller can't cut it short."""
        self._idle.clear()
        task = self._create_task(self._finish(), f"{__name__} continue playback")
        self._finishing = task

        def done(task: asyncio.Task[Outcome]) -> None:
            if self._finishing is task:
                self._finishing = None
            unawaited = task in self._unawaited
            self._unawaited.discard(task)
            if (
                not task.cancelled()
                and (err := task.exception()) is not None
                and (orphaned or unawaited)
            ):
                _LOGGER.warning("Kodi could not continue after the announcement: %s", err)
            self._update_idle()

        task.add_done_callback(done)
        return task

    async def _announce(
        self, url: str, volume: int | None, max_duration: float, start_timeout: float
    ) -> Outcome:
        if await self._muted():
            _LOGGER.info("Kodi is muted; announcement %s not played", redact_url(url))
            if self._session is not None and not self._waiting:
                await self._await_finish(self._start_finish())  # an earlier announcement played
            return Outcome.MUTED
        if self._session is None:
            self._session = await self._snapshot()
        session = self._session
        session.start_timeout = start_timeout
        clip_error: Exception | None = None
        override = False
        try:
            if volume is not None:
                volume = max(0, min(100, int(volume)))
                await self.client.call("Application.SetVolume", {"volume": volume})
                session.volume_set = volume
            override = await self._play_clip(session, url, max_duration, start_timeout)
        except (ClipError, KodiError) as err:
            clip_error = err
        if override:
            _LOGGER.info(
                "Kodi was used during the announcement; Better Kodi doesn't continue playback"
            )
            self._session = None
            with contextlib.suppress(KodiError):
                await self._restore_volume(session)
                await self._put_back_after_clip(session)
            return Outcome.OVERRIDE
        if self._waiting and clip_error is None:
            return Outcome.PASSED_ON
        outcome = await self._await_finish(self._start_finish())
        if clip_error is not None:
            raise clip_error
        return outcome

    async def _await_finish(self, task: asyncio.Task[Outcome]) -> Outcome:
        """Wait for Kodi to continue; if this caller is cancelled, the task goes on."""
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._unawaited.add(task)
            raise

    async def _finish(self) -> Outcome:
        """Continue what played before the announcements."""
        session, self._session = self._session, None
        assert session is not None
        try:
            await self._restore_volume(session)
            return await self._restore(session)
        except KodiError as err:
            raise RestoreError(str(err)) from err

    # --- Before ------------------------------------------------------------------

    async def _muted(self) -> bool:
        result = await self.client.call("Application.GetProperties", {"properties": ["muted"]})
        return bool((result or {}).get("muted"))

    async def _snapshot(self) -> Snapshot:
        app = await self.client.call("Application.GetProperties", {"properties": ["volume"]})
        snapshot = Snapshot(volume=(app or {}).get("volume"), playback=None)
        for playlist_id in PLAYLISTS:
            if (items := await self._playlist(playlist_id)) is not None:
                snapshot.playlists[playlist_id] = items
        player = await self._active_player()
        if player is None:
            return snapshot
        player_id, player_type = player
        properties = ["playlistid", "position", "time", "speed", "shuffled"]
        if player_type == "video":
            properties += STREAM_PROPERTIES
        props = await self.client.call(
            "Player.GetProperties", {"playerid": player_id, "properties": properties}
        )
        result = await self.client.call(
            "Player.GetItem", {"playerid": player_id, "properties": ["file"]}
        )
        item = (result or {}).get("item") or {}
        file = item.get("file") or None
        playback = Playback(kind="item", file=file, time=props.get("time"))
        playback.paused = props.get("speed") == 0
        playback.player_type = player_type
        playback.shuffled = bool(props.get("shuffled"))
        playback.audio = Stream.from_kodi(props.get("currentaudiostream"))
        playback.subtitle = Stream.from_kodi(props.get("currentsubtitle"))
        if "subtitleenabled" in props:
            playback.subtitle_enabled = bool(props["subtitleenabled"])
        playlist_id, position = props.get("playlistid"), props.get("position", -1)
        if item.get("type") == "channel" and item.get("id") is not None:
            playback.kind = "channel"
            playback.item = {"channelid": item["id"]}
            playback.time = None  # live: no time to continue at
        elif playlist_id in PLAYLISTS and position is not None and position >= 0:
            playback.kind = "playlist"
            playback.playlist_id, playback.position = playlist_id, int(position)
            if playlist_id not in snapshot.playlists:
                raise ClipError("The playing playlist can't be noted; not interrupting it")
        elif (key := LIBRARY_ID_KEYS.get(item.get("type", ""))) and item.get("id") is not None:
            playback.item = {key: item["id"]}
        elif file:
            playback.item = {"file": file}
        else:
            raise ClipError("What Kodi plays can't be opened again; not interrupting it")
        if playback.kind == "item":
            playback.playlist_current = await self._playlist_is_current(player_type)
        snapshot.playback = playback
        _LOGGER.debug("Before the announcement: %s", playback)
        return snapshot

    async def _playlist_is_current(self, player_type: str) -> bool:
        label = PLAYLIST_LENGTH_LABEL.get(player_type, PLAYLIST_LENGTH_LABEL["video"])
        try:
            result = await self.client.call("XBMC.GetInfoLabels", {"labels": [label]})
        except KodiRpcError:
            return False
        return bool((result or {}).get(label))

    async def _playlist(self, playlist_id: int) -> list[dict[str, Any]] | None:
        """Return a playlist as items to add again; None if an entry can't be added again."""
        result = await self.client.call(
            "Playlist.GetItems", {"playlistid": playlist_id, "properties": ["file"]}
        )
        items = [_same_kodi_item(raw) for raw in (result or {}).get("items") or []]
        if any(item is None for item in items):
            return None
        return [item for item in items if item is not None]

    async def _active_player(self) -> tuple[int, str] | None:
        players = await self.client.call("Player.GetActivePlayers") or []
        found = [
            (int(p["playerid"]), p.get("type", ""))
            for p in players
            if p.get("type") in ("video", "audio")
        ]
        found.sort(key=lambda player: player[1] != "video")
        return found[0] if found else None

    async def _playing_item(self, player_id: int) -> dict[str, Any] | None:
        try:
            result = await self.client.call(
                "Player.GetItem", {"playerid": player_id, "properties": ["file"]}
            )
        except KodiRpcError:
            return None  # the player stopped meanwhile
        return (result or {}).get("item") or {}

    async def _playing_file(self, player_id: int) -> str | None:
        return ((await self._playing_item(player_id)) or {}).get("file") or None

    # --- The clip ------------------------------------------------------------------

    async def _play_clip(
        self, session: Snapshot, url: str, max_duration: float, start_timeout: float
    ) -> bool:
        """Play the clip until it ends; return True if someone used Kodi meanwhile."""
        listening = self._listening = _Listening()
        try:
            opened_at = self._clock()
            session.clip = url
            await self.client.call("Player.Open", {"item": {"file": url}})
            if not await self._wait_for_clip(session, listening, url, opened_at, start_timeout):
                return await self._used_since(opened_at)  # played before it was seen
            override = await self._wait_for_clip_end(listening, url, opened_at, max_duration)
            # Input in the last moments of the clip counts too.
            return override or await self._used_since(opened_at)
        finally:
            self._listening = None

    async def _wait_for_clip(
        self,
        session: Snapshot,
        listening: _Listening,
        url: str,
        opened_at: float,
        start_timeout: float,
    ) -> bool:
        """Wait until the clip plays; False if it played and ended before that was seen."""
        deadline = opened_at + start_timeout
        unseen_since: float | None = None
        while True:
            await self._tick(listening)
            now = self._clock()
            player = await self._active_player()
            if player is not None and _same_file(await self._playing_file(player[0]), url):
                await self._repeat_off(session, player[0])
                return True
            if listening.stopped:
                return False
            if player is None and not self._notifications and await self._clip_queued(url):
                # Kodi puts the clip into the music playlist and plays it from there.
                # Without notifications, a clip shorter than a poll is never seen
                # playing: no player for a while means it is over.
                unseen_since = unseen_since if unseen_since is not None else now
                if now - unseen_since >= UNSEEN_CLIP_GRACE:
                    return False
            else:
                unseen_since = None
            if now >= deadline:
                raise ClipError(f"Kodi did not play {redact_url(url)}")

    async def _wait_for_clip_end(
        self, listening: _Listening, url: str, opened_at: float, max_duration: float
    ) -> bool:
        """Wait until the clip is over; return True if someone stopped it or used Kodi."""
        started_at = self._clock()
        next_idle_check = started_at + IDLE_CHECK_INTERVAL
        remaining: float | None = None
        gone_since: float | None = None
        while True:
            await self._tick(listening)
            now = self._clock()
            if listening.stopped:
                return listening.ended is False
            player = await self._active_player()
            if player is None:
                if self._notifications:
                    # Kodi's "stopped" (with "end") is on its way; wait for it a little.
                    gone_since = gone_since if gone_since is not None else now
                    if now - gone_since < STOP_NOTIFICATION_WAIT:
                        continue
                    return False
                # Without notifications: stopped well before its end means someone stopped it.
                return remaining is not None and remaining > END_TOLERANCE
            if not _same_file(await self._playing_file(player[0]), url):
                return True  # someone started something else
            remaining = await self._remaining(player[0])
            if now - started_at >= max_duration:
                _LOGGER.warning(
                    "Announcement %s still plays after %s s; stopping it",
                    redact_url(url),
                    max_duration,
                )
                await self.client.call("Player.Stop", {"playerid": player[0]})
                return False
            if now >= next_idle_check:
                next_idle_check = now + IDLE_CHECK_INTERVAL
                if await self._used_since(opened_at):
                    return True

    async def _clip_queued(self, url: str) -> bool:
        result = await self.client.call(
            "Playlist.GetItems", {"playlistid": MUSIC_PLAYLIST, "properties": ["file"]}
        )
        items = (result or {}).get("items") or []
        return len(items) == 1 and _same_file(items[0].get("file"), url)

    async def _repeat_off(self, session: Snapshot, player_id: int) -> None:
        """Play the clip once: repeat of the music playlist would loop it."""
        with contextlib.suppress(KodiRpcError):
            props = await self.client.call(
                "Player.GetProperties", {"playerid": player_id, "properties": ["repeat"]}
            )
            repeat = (props or {}).get("repeat")
            if repeat in ("one", "all"):
                await self.client.call("Player.SetRepeat", {"playerid": player_id, "repeat": "off"})
                if session.music_repeat is None:
                    session.music_repeat = repeat

    async def _tick(self, listening: _Listening) -> None:
        """Wait for the next poll, or less if Kodi notified something."""
        listening.changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(listening.changed.wait(), POLL_INTERVAL)

    async def _remaining(self, player_id: int) -> float | None:
        try:
            props = await self.client.call(
                "Player.GetProperties",
                {"playerid": player_id, "properties": ["time", "totaltime"]},
            )
        except KodiRpcError:
            return None
        total = _seconds(props.get("totaltime"))
        return total - _seconds(props.get("time")) if total else None

    async def _used_since(self, since: float) -> bool:
        """Return True if someone used Kodi (remote, keyboard, CEC) since then.

        Kodi counts the time since the last input; only input resets it, not
        what Better Kodi does (except Input.* calls).
        """
        seconds = int(self._clock() - since) - 1  # whole seconds, with a margin
        if seconds < 1:
            return False
        condition = f"System.IdleTime({seconds})"
        try:
            result = await self.client.call("XBMC.GetInfoBooleans", {"booleans": [condition]})
        except KodiRpcError:
            return False
        idle = (result or {}).get(condition)
        return idle is False

    # --- Afterwards ----------------------------------------------------------------

    async def _restore_volume(self, session: Snapshot) -> None:
        if session.volume is None or session.volume_set is None:
            return
        app = await self.client.call("Application.GetProperties", {"properties": ["volume"]})
        if (app or {}).get("volume") == session.volume_set:  # not changed by someone meanwhile
            await self.client.call("Application.SetVolume", {"volume": session.volume})

    async def _put_back(
        self,
        session: Snapshot,
        playlist_id: int,
        ours: list[dict[str, Any]],
        *,
        any_single: bool = False,
    ) -> None:
        """Put back a playlist that Kodi replaced when Better Kodi opened a single item.

        Only if it still holds just what Better Kodi opened: a playlist that
        someone or an add-on filled meanwhile is left alone.
        """
        if (saved := session.playlists.get(playlist_id)) is None:
            return  # it had entries that can't be added again
        current = await self._playlist(playlist_id)
        if current is None or current == saved:
            return
        # Once an add-on item plays, Kodi may show it by its resolved path or
        # library ID instead; a single entry is then still the reopened item.
        if not (_same_items(current, ours) or (any_single and len(current) == 1)):
            return
        # Kodi handles both in order; clearing doesn't stop what plays.
        await self.client.call("Playlist.Clear", {"playlistid": playlist_id})
        if saved:
            await self.client.call("Playlist.Add", {"playlistid": playlist_id, "item": saved})
        session.put_back.add(playlist_id)

    async def _keep_queue_after(
        self, session: Snapshot, playlist_id: int, playback: Playback, player_id: int
    ) -> None:
        """Put the playlist back in front of the reopened item, so Kodi stops after it.

        Before the announcement Kodi played the item outside the playlist and
        would stop after it. Kodi's JSON-RPC can only start an item as the
        only entry of its playlist, so the noted entries go back in front of
        it: Kodi plays nothing after the last entry. Once the item no longer
        plays, Better Kodi removes it from the playlist again.
        """
        saved = session.playlists.get(playlist_id)
        assert playback.item is not None
        current = await self._playlist(playlist_id)
        if saved is None or current is None or len(current) != 1:
            return  # entries that can't be added again, or someone changed the playlist
        try:
            props = await self.client.call(
                "Player.GetProperties",
                {"playerid": player_id, "properties": ["shuffled", "repeat"]},
            )
        except KodiRpcError:
            props = None  # the item stopped meanwhile
        if props is None or props.get("shuffled") or props.get("repeat") not in (None, "off"):
            # Kodi would reshuffle the inserted entries or go on after the last
            # one; the playlist is put back as it is, Kodi goes on with it.
            _LOGGER.debug("Kodi goes on with the playlist after the item (shuffle, repeat, stop)")
            await self._put_back(session, playlist_id, [playback.item], any_single=True)
            return
        # One entry per call: Kodi moves its current entry by one per insert,
        # also when one insert adds several entries.
        inserted: list[dict[str, Any]] = []
        seen: dict[str, Any] | None = None
        try:
            for entry in saved:
                try:
                    await self.client.call(
                        "Playlist.Insert",
                        {"playlistid": playlist_id, "position": len(inserted), "item": entry},
                    )
                except KodiRpcError as err:
                    # Gone meanwhile (a deleted library entry, a missing local file),
                    # as Kodi would have skipped it when playing the playlist.
                    _LOGGER.debug("Kodi refused an entry of the playlist: %s", err)
                else:
                    inserted.append(entry)
            # How Kodi shows the item now (an add-on item by its resolved path, say);
            # Kodi reads the playlist after handling the inserts (same message queue).
            shown = await self._playlist(playlist_id)
            if shown is not None and len(shown) > len(inserted):
                seen = shown[len(inserted)]
        finally:
            # Also if Kodi stopped answering halfway: the item's entry goes later.
            self._watch_tail(_Tail(playlist_id, inserted, [playback.item, seen]))

    def _watch_tail(self, tail: _Tail) -> None:
        task = self._create_task(self._remove_tail_later(tail), f"{__name__} remove reopened item")
        self._tail_tasks.add(task)
        task.add_done_callback(self._tail_tasks.discard)

    async def _remove_tail_later(self, tail: _Tail) -> None:
        """Remove the reopened item from its playlist once it no longer plays."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._tail_changed.wait(), TAIL_CHECK_INTERVAL)
            self._tail_changed.clear()
            # A check of its own, which an announcement cancels instead of waiting
            # for it (it is repeated later).
            check = asyncio.ensure_future(self._check_tail(tail))
            self._tail_checks.add(check)
            try:
                if await asyncio.shield(check):
                    return
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    check.cancel()
                    raise  # the watcher itself was cancelled (also with its check)
                # Only the check was cancelled (an announcement started): later again.
            finally:
                self._tail_checks.discard(check)

    async def _check_tail(self, tail: _Tail) -> bool:
        try:
            async with asyncio.timeout(TAIL_CHECK_TIMEOUT):
                await self.client.ping()  # only while Kodi answers
            async with asyncio.timeout(TAIL_CHECK_TIMEOUT), self.command():
                # Like a command: announcements and queue commands don't run meanwhile.
                return await self._remove_tail(tail)
        except (KodiError, TimeoutError) as err:
            _LOGGER.debug("Kodi not reachable to tidy up the playlist yet: %s", err)
            return False

    async def _remove_tail(self, tail: _Tail) -> bool:
        """Remove the item's entry unless it plays; True when done (or no longer ours)."""
        entries = await self._playlist(tail.playlist_id)
        index = len(tail.before)
        if (
            entries is None
            or len(entries) <= index
            or entries[:index] != tail.before
            or not any(_same_items([entries[index]], [item]) for item in tail.item if item)
        ):
            _LOGGER.debug("The playlist changed meanwhile; leaving it as it is")
            return True
        if (player := await self._active_player()) is not None:
            with contextlib.suppress(KodiRpcError):
                props = await self.client.call(
                    "Player.GetProperties",
                    {"playerid": player[0], "properties": ["playlistid", "position"]},
                )
                if (props.get("playlistid"), props.get("position")) == (tail.playlist_id, index):
                    return False  # still plays
        await self.client.call(
            "Playlist.Remove", {"playlistid": tail.playlist_id, "position": index}
        )
        _LOGGER.debug("Removed the reopened item from the playlist again")
        return True

    async def _put_back_after_clip(self, session: Snapshot) -> None:
        if session.clip is not None:
            # An audio clip replaces the music playlist, a video clip the queue.
            for playlist_id in PLAYLISTS:
                await self._put_back(session, playlist_id, [{"file": session.clip}])

    async def _stop_clip(self, session: Snapshot) -> None:
        """Stop the clip if it still plays (a cancelled announcement)."""
        if session.clip is None or (player := await self._active_player()) is None:
            return
        if not _same_file(await self._playing_file(player[0]), session.clip):
            return
        await self.client.call("Player.Stop", {"playerid": player[0]})
        listening = self._listening = _Listening(started=True)
        try:
            deadline = self._clock() + STOP_TIMEOUT
            while await self._active_player() is not None and self._clock() < deadline:
                await self._tick(listening)
        finally:
            self._listening = None

    async def _restore(self, session: Snapshot) -> Outcome:
        await self._stop_clip(session)
        await self._put_back_after_clip(session)
        playback = session.playback
        if playback is None:
            return Outcome.DONE
        if await self._active_player() is not None:
            _LOGGER.info("Kodi plays something else now; Better Kodi doesn't continue playback")
            return Outcome.OVERRIDE
        # A playlist put back while shuffled was shuffled anew by Kodi: open it in
        # the noted order and shuffle again once it plays.
        reshuffle = (
            playback.kind == "playlist"
            and playback.shuffled
            and playback.playlist_id in session.put_back
        )
        replaced: int | None = None  # the playlist Kodi replaces when opening a single item
        if playback.kind == "playlist":
            params: dict[str, Any] = {
                "item": {"playlistid": playback.playlist_id, "position": playback.position}
            }
            if reshuffle:
                params["options"] = {"shuffled": False}
        else:
            assert playback.item is not None
            params = {"item": playback.item}
            if playback.seconds:
                params["options"] = {"resume": playback.time}
            if playback.kind == "item":
                replaced = VIDEO_PLAYLIST if playback.player_type == "video" else MUSIC_PLAYLIST
        await self.client.call("Player.Open", params)
        player_id: int | None = None
        try:
            player_id = await self._wait_for_start(playback, session.start_timeout)
        finally:
            if replaced is not None:
                # Kodi plays the item from the playlist it replaced; put the old one back.
                with contextlib.suppress(KodiError):
                    if not playback.playlist_current and player_id is not None:
                        try:
                            await self._keep_queue_after(session, replaced, playback, player_id)
                        except KodiError:
                            await self._put_back(
                                session, replaced, [playback.item], any_single=True
                            )
                    else:
                        # Kodi went on with the playlist after the item: it still does.
                        # (Also if the item didn't start: the playlist mustn't get lost.)
                        await self._put_back(session, replaced, [playback.item], any_single=True)
        if player_id is None:
            return Outcome.OVERRIDE
        if playback.kind != "channel" and playback.seconds:
            await self._seek(player_id, playback)
        if playback.audio or playback.subtitle:
            await self._restore_streams(player_id, playback)
        if (
            playback.kind == "playlist"
            and playback.playlist_id == MUSIC_PLAYLIST
            and session.music_repeat is not None
        ):
            await _try(
                self.client.call(
                    "Player.SetRepeat", {"playerid": player_id, "repeat": session.music_repeat}
                )
            )
        if reshuffle:
            await _try(
                self.client.call("Player.SetShuffle", {"playerid": player_id, "shuffle": True})
            )
        if playback.paused:
            await self.client.call("Player.PlayPause", {"playerid": player_id, "play": False})
        return Outcome.DONE

    async def _wait_for_start(self, playback: Playback, start_timeout: float) -> int | None:
        """Wait until Kodi plays the noted item again; answer its resume dialog.

        Returns the player ID, or None if someone used Kodi meanwhile.
        """
        deadline = self._clock() + start_timeout
        since = next_idle_check = self._clock()
        answered = False
        listening = self._listening = _Listening()
        try:
            while True:
                await self._tick(listening)
                player = await self._active_player()
                if player is not None and await self._plays_noted(player[0], playback):
                    return player[0]
                if (
                    player is None
                    and not answered
                    and playback.seconds
                    and await self._answer_resume_dialog(playback.seconds)
                ):
                    answered = True
                    since = self._clock()  # Input.Select counts as input
                elif self._clock() >= next_idle_check:
                    next_idle_check = self._clock() + IDLE_CHECK_INTERVAL
                    if await self._used_since(since):
                        return None
                if self._clock() >= deadline:
                    raise RestoreError(
                        f"Kodi did not start {redact_url(playback.file or str(playback.item))} "
                        f"again within {start_timeout:g} s"
                    )
        finally:
            self._listening = None

    async def _plays_noted(self, player_id: int, playback: Playback) -> bool:
        """Return True if the player plays the noted item.

        A single item can't be compared reliably (add-ons resolve their paths
        to stream URLs), so it counts; someone starting something else is
        caught by the input check.
        """
        if playback.kind == "playlist":
            try:
                props = await self.client.call(
                    "Player.GetProperties",
                    {"playerid": player_id, "properties": ["playlistid", "position"]},
                )
            except KodiRpcError:
                return False
            return (props.get("playlistid"), props.get("position")) == (
                playback.playlist_id,
                playback.position,
            )
        if playback.kind == "channel":
            item = await self._playing_item(player_id) or {}
            return item.get("type") == "channel" and {"channelid": item.get("id")} == playback.item
        return True

    async def _answer_resume_dialog(self, seconds: int) -> bool:
        """Choose "Resume from …" in a dialog, if one offers exactly the noted time.

        Kodi asks only when started from its own menus, but add-ons may ask
        too (Jellyfin for Kodi when transcoding). Better Kodi only confirms
        the focused entry if it shows the noted time; the wording doesn't
        matter (any language).
        """
        try:
            gui = await self.client.call(
                "GUI.GetProperties", {"properties": ["currentwindow", "currentcontrol"]}
            )
        except KodiRpcError:
            return False
        label = ((gui or {}).get("currentcontrol") or {}).get("label") or ""
        for match in TIME_IN_TEXT.finditer(label):
            hours, minutes, secs = (int(part) if part else 0 for part in match.groups())
            if abs(hours * 3600 + minutes * 60 + secs - seconds) <= DIALOG_TIME_TOLERANCE:
                _LOGGER.debug("Confirming %r to continue playback", label)
                await self.client.call("Input.Select")
                return True
        return False

    async def _seek(self, player_id: int, playback: Playback) -> None:
        deadline = self._clock() + SEEK_TIMEOUT
        while True:
            try:
                props = await self.client.call(
                    "Player.GetProperties", {"playerid": player_id, "properties": ["time"]}
                )
                if abs(_seconds(props.get("time")) - playback.seconds) <= SEEK_TOLERANCE:
                    return  # Kodi started there itself
                await self.client.call(
                    "Player.Seek", {"playerid": player_id, "value": {"time": playback.time}}
                )
            except KodiRpcError as err:
                _LOGGER.debug("Seek not possible yet: %s", err)
            else:
                return
            if self._clock() >= deadline:
                raise RestoreError("Kodi started again, but did not accept seeking in time")
            await asyncio.sleep(POLL_INTERVAL)

    async def _restore_streams(self, player_id: int, playback: Playback) -> None:
        """Choose the same audio track and subtitle again.

        Kodi remembers them per file itself; this covers streams whose address
        changes (add-ons, media servers). Kodi lists the streams only once the
        stream is open, so this waits for them a while.
        """
        deadline = self._clock() + STREAMS_TIMEOUT
        while True:
            try:
                props = await self.client.call(
                    "Player.GetProperties",
                    {"playerid": player_id, "properties": STREAM_PROPERTIES},
                )
            except KodiRpcError:
                return
            if props.get("audiostreams") or props.get("subtitles"):
                break
            if self._clock() >= deadline:
                _LOGGER.debug("Kodi listed no audio tracks or subtitles; leaving them")
                return
            await asyncio.sleep(POLL_INTERVAL)
        await _try(self._set_audio(player_id, playback, props))
        await _try(self._set_subtitle(player_id, playback, props))

    async def _set_audio(self, player_id: int, playback: Playback, props: dict[str, Any]) -> None:
        if playback.audio is None:
            return
        index = playback.audio.find_in(props.get("audiostreams"))
        current = Stream.from_kodi(props.get("currentaudiostream"))
        if index is not None and (current is None or current.index != index):
            await self.client.call(
                "Player.SetAudioStream", {"playerid": player_id, "stream": index}
            )

    async def _set_subtitle(
        self, player_id: int, playback: Playback, props: dict[str, Any]
    ) -> None:
        current = Stream.from_kodi(props.get("currentsubtitle"))
        enabled = bool(props.get("subtitleenabled"))
        if playback.subtitle is not None:
            index = playback.subtitle.find_in(props.get("subtitles"))
            if index is not None and (current is None or current.index != index):
                await self.client.call(
                    "Player.SetSubtitle",
                    {
                        "playerid": player_id,
                        "subtitle": index,
                        "enable": bool(playback.subtitle_enabled),
                    },
                )
                return
        if playback.subtitle_enabled is not None and playback.subtitle_enabled != enabled:
            await self.client.call(
                "Player.SetSubtitle",
                {"playerid": player_id, "subtitle": "on" if playback.subtitle_enabled else "off"},
            )


async def _try(operation: Awaitable[Any]) -> None:
    """Run a finishing touch; a refusal must not fail the announcement."""
    try:
        await operation
    except KodiRpcError as err:
        _LOGGER.debug("Kodi refused: %s", err)


def _seconds(value: dict[str, int] | None) -> float:
    if not value:
        return 0
    return (
        value.get("hours", 0) * 3600
        + value.get("minutes", 0) * 60
        + value.get("seconds", 0)
        + value.get("milliseconds", 0) / 1000
    )


def _same_file(playing: str | None, url: str) -> bool:
    if not playing:
        return False
    return playing == url or unquote(playing) == unquote(url)


# Playlist entries as items to add again on the same Kodi: library entries by ID
# (keeps their library link), everything else by path.
SAME_KODI_ID_KEYS = {**LIBRARY_ID_KEYS, "song": "songid"}


def _same_kodi_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    key = SAME_KODI_ID_KEYS.get(raw.get("type") or "")
    if key and isinstance(raw.get("id"), int) and raw["id"] >= 0:
        return {key: raw["id"]}
    if file := raw.get("file"):
        return {"file": file}
    return None


def _same_items(current: list[dict[str, Any]], ours: list[dict[str, Any] | None]) -> bool:
    """Return True if a playlist holds just the given items (by ID or path)."""
    if len(current) != len(ours):
        return False
    for have, want in zip(current, ours, strict=True):
        if want is None:
            return False
        if have != want and not (
            have.get("file") and _same_file(have.get("file"), want.get("file") or "")
        ):
            return False
    return True
