"""A small in-memory Kodi that answers the JSON-RPC calls Better Kodi uses."""

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from custom_components.better_kodi.kodi_client import KodiConnectionError, KodiRpcError

VIDEO = 1


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
        }

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

    # JSON-RPC --------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Answer one JSON-RPC call."""
        if self.offline:
            raise KodiConnectionError(f"{method}: offline")
        params = deepcopy(params or {})
        self.calls.append((method, params))
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
            [{"playerid": 1, "playertype": "internal", "type": "video"}]
            if self.player_active
            else []
        )

    def _Player_GetProperties(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        self._require_player(playerid)
        in_queue = self.position >= 0
        return {
            "playlistid": VIDEO if in_queue else -1,
            "position": self.position,
            "time": dict(self.time),
        }

    def _Player_GetItem(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        self._require_player(playerid)
        item = self.playlist[self.position] if self.position >= 0 else self.current_outside
        return {"item": dict(item or {})}

    def _Player_Open(self, item: dict[str, Any], **_: Any) -> str:
        if item.get("playlistid") == VIDEO:
            position = item.get("position", 0)
            if not 0 <= position < len(self.playlist):
                raise KodiRpcError("Player.Open", -32602, "Invalid params.")
            self.position = position
            self.time = {"hours": 0, "minutes": 0, "seconds": 0, "milliseconds": 0}
            if self.start_delay:
                self.player_active = False
                self._pending_start = self.start_delay
            else:
                self.player_active = True
            return "OK"
        raise KodiRpcError("Player.Open", -32602, "Unsupported item")

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
        self.player_active = False
        self.position = -1
        return "OK"

    def _Playlist_GetProperties(self, playlistid: int, properties: list[str]) -> dict[str, Any]:
        return {"size": len(self.playlist), "type": "video"}

    def _Playlist_GetItems(
        self, playlistid: int, properties: list[str] | None = None
    ) -> dict[str, Any]:
        items = [dict(item) for item in self.playlist]
        return {"items": items, "limits": {"start": 0, "end": len(items), "total": len(items)}}

    def _Playlist_Add(self, playlistid: int, item: Any) -> str:
        for entry in item if isinstance(item, list) else [item]:
            self.playlist.append(self._to_playlist_item(entry))
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
        self.playlist.clear()
        if self.position >= 0:
            self.position = -1
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
        if not self.player_active or playerid != 1:
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
