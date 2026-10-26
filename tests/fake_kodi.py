"""A small in-memory Kodi that answers the JSON-RPC calls Better Kodi uses."""

from collections.abc import Callable
from copy import deepcopy
import time
from typing import Any
from urllib.parse import urlsplit

from custom_components.better_kodi.kodi_client import KodiConnectionError, KodiRpcError

MUSIC = 0
VIDEO = 1
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg")


class FakeKodi:
    """Simulate Kodi's video playlist and player."""

    def __init__(self) -> None:
        """Start idle with an empty queue."""
        self.playlist: list[dict[str, Any]] = []
        self.player_active = False
        self.position = -1  # -1: playing something that is not from the queue
        self.current_outside: dict[str, Any] | None = None
        self.time = {"hours": 0, "minutes": 0, "seconds": 0, "milliseconds": 0}
        self.addons: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.offline = False
        self.seeks: list[dict[str, Any]] = []
        # Like real Kodi, a player can need a while to show up after Player.Open,
        # and may refuse to seek right after starting.
        self.start_delay = 0
        self.seek_refusals = 0
        self._pending_start: int | None = None
        # Kodi's video library: "movies", "episodes", "musicvideos" -> items with "file"
        self.library: dict[str, list[dict[str, Any]]] = {}
        # Network.MacAddress answers in order; the last one repeats ("Busy" first, like Kodi)
        self.mac_answers = ["Busy", "aa:bb:cc:dd:ee:ff"]
        self.power: dict[str, bool] = {
            "cansuspend": True,
            "canhibernate": True,
            "canshutdown": True,
            "canreboot": True,
        }
        self.notifications: list[dict[str, Any]] = []
        # Announcements: an audio player, volume, Kodi's idle timer, streams
        self.player_type = "video"
        self.music_playlist: list[dict[str, Any]] = []
        self.volume = 50
        self.muted = False
        self.speed = 1
        self.clip_length = 0.6  # seconds a clip plays
        self.clip_starts = True
        self._clip: tuple[str, float] | None = None  # (file, started)
        self.clips: list[str] = []
        self.idle_since = time.monotonic() - 3600
        self.audio_streams: list[dict[str, Any]] = []
        self.audio_index = 0
        self.subtitles: list[dict[str, Any]] = []
        self.subtitle_index = 0
        self.subtitle_enabled = False
        self.music_repeat = "off"  # repeat of the music playlist ("off", "one", "all")
        # Shuffle of the music playlist: adding while shuffled shuffles everything
        # again (here: reversed, to be predictable); opening unshuffled restores
        # the order the entries were added in.
        self.music_shuffled = False
        self._music_added: list[dict[str, Any]] = []
        # Called after Kodi opened a single item (an add-on filling the playlist, say)
        self.after_open_single: Callable[[], None] | None = None
        self.forget_streams = False  # opening again starts with the first tracks
        self.streams_delay = 0  # answers without streams after opening (stream not open yet)
        self._streams_hidden = 0
        self.websocket = False
        self.resume_dialog = False  # an add-on asks "Resume from …" when an item opens
        self._dialog: dict[str, Any] | None = None
        self.window_label = "Home"
        # Kodi's notifications over WebSocket, if a test listens
        self.notify: Callable[[str, dict[str, Any]], None] | None = None

    # Helpers for tests ----------------------------------------------------------

    def files(self) -> list[str | None]:
        """Return the file paths in the queue."""
        return [item.get("file") for item in self.playlist]

    def play_outside_queue(self, file: str) -> None:
        """Pretend something was started on the TV, not from the queue."""
        self.player_active = True
        self.position = -1
        self.current_outside = {"file": file, "label": file, "type": "unknown"}

    def method_calls(self) -> list[str]:
        """Return the called method names."""
        return [method for method, _ in self.calls]

    def press_key(self) -> None:
        """Someone uses the remote."""
        self.idle_since = time.monotonic()

    def user_stops(self) -> None:
        """Someone presses stop."""
        self.press_key()
        self._stop(end=False)

    def user_plays(self, file: str) -> None:
        """Someone starts something else on the TV."""
        self.press_key()
        self._clip = None
        self.play_outside_queue(file)
        self.player_type = "video"
        self._emit("Player.OnPlay")

    def start_video_outside_queue(self, item: dict[str, Any], seconds: int = 0) -> None:
        """Pretend a library item or file plays that was not started from the queue."""
        self.player_active = True
        self.player_type = "video"
        self.position = -1
        self.current_outside = dict(item)
        self.time = _time(seconds)

    def _emit(self, method: str, **data: Any) -> None:
        if self.notify is not None:
            self.notify(method, data)

    def _stop(self, *, end: bool) -> None:
        if not self.player_active:
            return
        self.player_active = False
        self.position = -1
        self._clip = None
        self.player_type = "video"
        self._emit("Player.OnStop", end=end, item={"type": "unknown"})

    def _advance(self) -> None:
        """Let a clip play on; it ends by itself after clip_length."""
        if self._clip is not None and time.monotonic() - self._clip[1] >= self.clip_length:
            if self.music_repeat != "off":
                self._clip = (self._clip[0], time.monotonic())  # plays again, gaplessly
            else:
                self._stop(end=True)

    # JSON-RPC --------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Answer one JSON-RPC call."""
        if self.offline:
            raise KodiConnectionError(f"{method}: offline")
        params = deepcopy(params or {})
        self.calls.append((method, params))
        self._advance()
        handler = getattr(self, "_" + method.replace(".", "_"), None)
        if handler is None:
            raise KodiRpcError(method, -32601, "Method not found.")
        return handler(**params)

    def _JSONRPC_Ping(self) -> str:
        return "pong"

    def _XBMC_GetInfoLabels(self, labels: list[str]) -> dict[str, str]:
        answer = self.mac_answers.pop(0) if len(self.mac_answers) > 1 else self.mac_answers[0]
        return {label: answer if label == "Network.MacAddress" else "" for label in labels}

    def _System_GetProperties(self, properties: list[str]) -> dict[str, bool]:
        return {name: self.power.get(name, False) for name in properties}

    def _System_Suspend(self) -> str:
        return "OK"

    def _System_Hibernate(self) -> str:
        return "OK"

    def _System_Shutdown(self) -> str:
        return "OK"

    def _Application_Quit(self) -> str:
        return "OK"

    def _System_Reboot(self) -> str:
        return "OK"

    def _VideoLibrary_Scan(self, **_: Any) -> str:
        return "OK"

    def _VideoLibrary_Clean(self, **_: Any) -> str:
        return "OK"

    def _GUI_ShowNotification(self, **params: Any) -> str:
        self.notifications.append(params)
        return "OK"

    def _Addons_GetAddons(self, **_: Any) -> dict[str, Any]:
        return {
            "addons": [
                {"addonid": addon, "type": "xbmc.python.pluginsource"}
                for addon in sorted(self.addons)
            ]
        }

    def _Player_GetActivePlayers(self) -> list[dict[str, Any]]:
        if self._pending_start is not None:
            if self._pending_start > 0:
                self._pending_start -= 1
            else:
                self._pending_start = None
                self.player_active = True
        return (
            [{"playerid": self._player_id, "playertype": "internal", "type": self.player_type}]
            if self.player_active
            else []
        )

    @property
    def _player_id(self) -> int:
        return 0 if self.player_type == "audio" else 1

    def _Player_GetProperties(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        self._require_player(playerid)
        in_queue = self.position >= 0
        props: dict[str, Any] = {
            "playlistid": (MUSIC if self.player_type == "audio" else VIDEO) if in_queue else -1,
            "position": self.position,
            "time": dict(self.time),
            "speed": self.speed,
            "repeat": self.music_repeat if self.player_type == "audio" else "off",
            "shuffled": self.music_shuffled if self.player_type == "audio" else False,
            "totaltime": _time(0),
        }
        if self._clip is not None:
            played = time.monotonic() - self._clip[1]
            props["time"] = _time(int(played))
            props["totaltime"] = _time(max(1, round(self.clip_length)))
        hidden = self._streams_hidden > 0 and any(
            name in properties for name in ("audiostreams", "subtitles")
        )
        if hidden:
            self._streams_hidden -= 1
        if self.player_type == "video" and hidden:
            props |= {"audiostreams": [], "subtitles": [], "currentaudiostream": {}}
            props |= {"currentsubtitle": {}, "subtitleenabled": False}
        elif self.player_type == "video":
            props["audiostreams"] = [dict(s) for s in self.audio_streams]
            props["currentaudiostream"] = self._stream(self.audio_streams, self.audio_index)
            props["subtitles"] = [dict(s) for s in self.subtitles]
            props["currentsubtitle"] = self._stream(self.subtitles, self.subtitle_index)
            props["subtitleenabled"] = self.subtitle_enabled
        return {name: props[name] for name in properties if name in props}

    @staticmethod
    def _stream(streams: list[dict[str, Any]], index: int) -> dict[str, Any]:
        for stream in streams:
            if stream["index"] == index:
                return dict(stream)
        return {}

    def _Player_GetItem(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        self._require_player(playerid)
        playlist = self.music_playlist if self.player_type == "audio" else self.playlist
        item = playlist[self.position] if self.position >= 0 else self.current_outside
        return {"item": dict(item or {})}

    def _Player_Open(self, item: dict[str, Any], options: dict[str, Any] | None = None) -> str:
        """Like Kodi: a single item replaces its playlist (music or video) first."""
        if item.get("playlistid") == MUSIC:
            position = item.get("position", 0)
            if not 0 <= position < len(self.music_playlist):
                raise KodiRpcError("Player.Open", -32602, "Invalid params.")
            if (options or {}).get("shuffled") is False and self.music_shuffled:
                self.music_playlist[:] = self._music_added
                self.music_shuffled = False
            self._stop(end=False)
            self.player_type, self.position, self.player_active = "audio", position, True
            self.speed = 1
            self._emit("Player.OnPlay")
            return "OK"
        if "playlistid" not in item:
            return self._open_single(item, (options or {}).get("resume"))
        self._stop(end=False)
        self._clip = None
        self.player_type = "video"
        self.speed = 1  # opening plays, also what was paused
        if item.get("playlistid") == VIDEO:
            position = item.get("position", 0)
            if not 0 <= position < len(self.playlist):
                raise KodiRpcError("Player.Open", -32602, "Invalid params.")
            self.position = position
            self.time = {"hours": 0, "minutes": 0, "seconds": 0, "milliseconds": 0}
            self._forget_streams()
            if self.start_delay:
                self.player_active = False
                self._pending_start = self.start_delay
            else:
                self.player_active = True
                self._emit("Player.OnPlay")
            return "OK"
        raise KodiRpcError("Player.Open", -32602, "Unsupported item")

    def _open_single(self, item: dict[str, Any], resume: Any) -> str:
        """Like Kodi's PlayListPlayer::Play(item): its playlist becomes just this item.

        Live TV goes through PVR and leaves the playlists alone.
        """
        file = item.get("file")
        self._stop(end=False)
        if file and file.lower().endswith(AUDIO_EXTENSIONS):
            self.music_playlist[:] = [self._to_playlist_item(item)]
            self._music_added[:] = list(self.music_playlist)
            self.clips.append(file)
            if not self.clip_starts:
                return "OK"  # added, but the player fails
            self._clip = (file, time.monotonic())
            self.player_type, self.position, self.player_active = "audio", 0, True
            self._emit("Player.OnPlay")
            self._emit("Player.OnAVStart")
            return "OK"
        if "channelid" in item:
            channel = {"type": "channel", "id": item["channelid"], "file": "pvr://channels/1"}
            self._start(channel, -1, 0)
            return "OK"
        self.playlist[:] = [self._to_playlist_item(item)]
        if self.after_open_single is not None:
            self.after_open_single()
        seconds = _seconds(resume) if isinstance(resume, dict) else 0
        if self.resume_dialog and seconds:
            # The add-on asks first; Input.Select confirms "Resume from …"
            minutes, secs = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            self._dialog = {
                "seconds": seconds,
                "label": f"Fortsetzen ab {hours}:{minutes:02}:{secs:02}",
            }
            return "OK"
        self._start(None, 0, seconds)
        return "OK"

    def _start(self, outside: dict[str, Any] | None, position: int, seconds: int) -> None:
        self.player_active, self.player_type = True, "video"
        self.position, self.current_outside = position, outside
        self.time = _time(seconds)
        self.speed = 1
        self._forget_streams()
        self._emit("Player.OnPlay")

    def _forget_streams(self) -> None:
        self._streams_hidden = self.streams_delay
        if self.forget_streams:
            self.audio_index, self.subtitle_index, self.subtitle_enabled = 0, 0, False

    def _Player_SetRepeat(self, playerid: int, repeat: str) -> str:
        self._require_player(playerid)
        if self.player_type == "audio":
            self.music_repeat = repeat
        return "OK"

    def _Player_SetShuffle(self, playerid: int, shuffle: bool) -> str:
        self._require_player(playerid)
        if self.player_type == "audio":
            self.music_shuffled = shuffle
        return "OK"

    def _Player_PlayPause(self, playerid: int, play: Any = "toggle") -> dict[str, Any]:
        self._require_player(playerid)
        self.speed = 0 if play is False or (play == "toggle" and self.speed) else 1
        return {"speed": self.speed}

    def _Player_SetAudioStream(self, playerid: int, stream: Any) -> str:
        self._require_player(playerid)
        self.audio_index = int(stream)
        return "OK"

    def _Player_SetSubtitle(self, playerid: int, subtitle: Any, enable: bool = False) -> str:
        self._require_player(playerid)
        if subtitle == "off":
            self.subtitle_enabled = False
        elif subtitle == "on":
            self.subtitle_enabled = True
        else:
            self.subtitle_index = int(subtitle)
            self.subtitle_enabled = enable
        return "OK"

    def _Application_GetProperties(self, properties: list[str]) -> dict[str, Any]:
        values = {"volume": self.volume, "muted": self.muted}
        return {name: values[name] for name in properties}

    def _Application_SetVolume(self, volume: int) -> int:
        self.volume = volume
        return volume

    def _XBMC_GetInfoBooleans(self, booleans: list[str]) -> dict[str, bool]:
        answers = {}
        for condition in booleans:
            assert condition.startswith("System.IdleTime(")
            seconds = int(condition.removeprefix("System.IdleTime(").rstrip(")"))
            answers[condition] = time.monotonic() - self.idle_since >= seconds
        return answers

    def _GUI_GetProperties(self, properties: list[str]) -> dict[str, Any]:
        if self._dialog is not None:
            return {
                "currentwindow": {"id": 13001, "label": ""},
                "currentcontrol": {"label": self._dialog["label"]},
            }
        return {
            "currentwindow": {"id": 10000, "label": self.window_label},
            "currentcontrol": {"label": ""},
        }

    def _Input_Select(self) -> str:
        self.press_key()  # like Kodi: counts as input
        if self._dialog is not None:
            dialog, self._dialog = self._dialog, None
            self._start(None, 0, dialog["seconds"])
        return "OK"

    def _Player_GoTo(self, playerid: int, to: int) -> str:
        self._require_player(playerid)
        self.position = to
        return "OK"

    def _Player_Seek(self, playerid: int, value: dict[str, Any]) -> dict[str, Any]:
        self._require_player(playerid)
        if self.seek_refusals:
            self.seek_refusals -= 1
            raise KodiRpcError("Player.Seek", -32100, "Failed to execute method.")
        self.seeks.append(value)
        self.time = dict(value["time"])
        return {"time": self.time}

    def _Player_Stop(self, playerid: int) -> str:
        self._require_player(playerid)
        self._stop(end=False)
        return "OK"

    def _list(self, playlistid: int) -> list[dict[str, Any]]:
        return self.music_playlist if playlistid == MUSIC else self.playlist

    def _Playlist_GetProperties(self, playlistid: int, properties: list[str]) -> dict[str, Any]:
        return {"size": len(self._list(playlistid)), "type": "video"}

    def _Playlist_GetItems(
        self, playlistid: int, properties: list[str] | None = None
    ) -> dict[str, Any]:
        items = [dict(item) for item in self._list(playlistid)]
        return {"items": items, "limits": {"start": 0, "end": len(items), "total": len(items)}}

    def _Playlist_Add(self, playlistid: int, item: Any) -> str:
        for entry in item if isinstance(item, list) else [item]:
            self._list(playlistid).append(self._to_playlist_item(entry))
            if playlistid == MUSIC:
                self._music_added.append(self._to_playlist_item(entry))
        if playlistid == MUSIC and self.music_shuffled:
            self.music_playlist.reverse()
        return "OK"

    def _Playlist_Insert(self, playlistid: int, position: int, item: Any) -> str:
        if not 0 <= position <= len(self.playlist):
            raise KodiRpcError("Playlist.Insert", -32602, "Invalid params.")
        self.playlist.insert(position, self._to_playlist_item(item))
        if self.position >= 0 and position <= self.position:
            self.position += 1
        return "OK"

    def _Playlist_Remove(self, playlistid: int, position: int) -> str:
        if position == self.position:
            raise KodiRpcError("Playlist.Remove", -32602, "Invalid params.")
        del self.playlist[position]
        if position < self.position:
            self.position -= 1
        return "OK"

    def _Playlist_Swap(self, playlistid: int, position1: int, position2: int) -> str:
        self.playlist[position1], self.playlist[position2] = (
            self.playlist[position2],
            self.playlist[position1],
        )
        if self.position == position1:
            self.position = position2
        elif self.position == position2:
            self.position = position1
        return "OK"

    def _Playlist_Clear(self, playlistid: int) -> str:
        """Like Kodi: what plays from it goes on, without a position."""
        playing = self.music_playlist if self.player_type == "audio" else self.playlist
        if self.position >= 0 and self._list(playlistid) is playing:
            self.current_outside = playing[self.position]
            self.position = -1
        self._list(playlistid).clear()
        if playlistid == MUSIC:
            self._music_added.clear()
        return "OK"

    def _library(self, key: str, filter: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        items = self.library.get(key, [])
        if filter:
            assert filter["field"] == "filename"
            value = filter["value"]

            def file_name(item: dict[str, Any]) -> str:
                # Kodi's "filename" is the part after the last "/" of the file.
                return item["file"].rsplit("/", 1)[-1]

            if filter["operator"] == "contains":
                items = [item for item in items if value in file_name(item)]
            elif filter["operator"] == "is":
                items = [item for item in items if file_name(item) == value]
        return {key: [dict(item) for item in items], "limits": {"total": len(items)}}

    def _VideoLibrary_GetMovies(self, **params: Any) -> dict[str, Any]:
        return self._library("movies", **params)

    def _VideoLibrary_GetEpisodes(self, **params: Any) -> dict[str, Any]:
        return self._library("episodes", **params)

    def _VideoLibrary_GetMusicVideos(self, **params: Any) -> dict[str, Any]:
        return self._library("musicvideos", **params)

    # Internals --------------------------------------------------------------------

    def _require_player(self, playerid: int) -> None:
        if not self.player_active or playerid != self._player_id:
            raise KodiRpcError("Player", -32100, "Failed to execute method.")

    @staticmethod
    def _to_playlist_item(entry: dict[str, Any]) -> dict[str, Any]:
        if "file" in entry:
            # Kodi labels unplayed paths with their last path segment, for example
            # "play" for plugin://plugin.video.youtube/play/?video_id=…
            path = urlsplit(entry["file"]).path.rstrip("/")
            label = path.rsplit("/", 1)[-1] or entry["file"]
            return {"file": entry["file"], "label": label, "type": "unknown"}
        for key, kind in (
            ("movieid", "movie"),
            ("episodeid", "episode"),
            ("musicvideoid", "musicvideo"),
        ):
            if key in entry:
                return {
                    "id": entry[key],
                    "type": kind,
                    "label": f"{kind} {entry[key]}",
                    "title": f"{kind} {entry[key]}",
                    "file": f"smb://nas/{kind}/{entry[key]}.mkv",
                }
        raise KodiRpcError("Playlist.Add", -32602, "Invalid params.")


def _time(seconds: int) -> dict[str, int]:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return {"hours": hours, "minutes": minutes, "seconds": secs, "milliseconds": 0}


def _seconds(value: dict[str, int]) -> int:
    return value.get("hours", 0) * 3600 + value.get("minutes", 0) * 60 + value.get("seconds", 0)
