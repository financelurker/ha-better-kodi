"""A simulated Kodi for README screenshots: JSON-RPC over HTTP and WebSocket.

It reuses the in-memory Kodi of the tests and adds demo content: a small video
library with invented titles and generated covers, and a queue that plays.

    python scripts/screenshots/fake_kodi_server.py [--port 8080] [--ws-port 9090]
"""

import argparse
import asyncio
import colorsys
import io
import json
import logging
from pathlib import Path
import sys
from typing import Any
from urllib.parse import unquote

from aiohttp import WSMsgType, web
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custom_components.better_kodi.kodi_client import KodiError, KodiRpcError  # noqa: E402
from tests.fake_kodi import FakeKodi  # noqa: E402

_LOGGER = logging.getLogger("fake_kodi")

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Invented titles only.
MOVIES = [
    (11, "The Last Ferry", 2023),
    (12, "Copper Valley", 2021),
    (13, "Winter Orchard", 2024),
    (14, "Signals from the North", 2022),
    (15, "Paper Lanterns", 2020),
    (16, "The Quiet Harbour", 2025),
]
SHOW = (21, "Northbound")
EPISODES = [
    (31, 1, 1, "Departure"),
    (32, 1, 2, "The Pass"),
    (33, 1, 3, "Snowline"),
    (34, 1, 4, "Base Camp"),
]


def image_path(name: str) -> str:
    return f"image://special%3a%2f%2fdemo%2f{name}.png/"


def poster(name: str, title: str, hue: float, size: tuple[int, int] = (400, 600)) -> bytes:
    """Draw a simple cover: a two-color gradient, a circle and the title."""
    width, height = size
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.85))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.08) % 1, 0.75, 0.30))
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)),
        )
    r = width // 4
    draw.ellipse(
        (width // 2 - r, height // 3 - r, width // 2 + r, height // 3 + r),
        fill=tuple(min(255, c + 40) for c in top),
    )
    size = max(18, width // 13)
    font = ImageFont.truetype(FONT, size) if FONT.exists() else ImageFont.load_default(size)
    words, lines, line = title.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) > width * 0.85 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    y = int(height * 0.68)
    for text in lines:
        w = draw.textlength(text, font=font)
        draw.text(((width - w) / 2, y), text, font=font, fill=(255, 255, 255))
        y += int(font.size * 1.25)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class DemoKodi(FakeKodi):
    """The test Kodi with demo content and the calls the Kodi integration makes."""

    def __init__(self) -> None:
        super().__init__()
        self.images: dict[str, bytes] = {}
        self.addons = {"plugin.video.youtube", "plugin.video.jellyfin"}
        self.mac_answers = ["AA:BB:CC:12:34:56"]
        movies = []
        for index, (movie_id, title, year) in enumerate(MOVIES):
            name = f"movie{movie_id}"
            self.images[name] = poster(name, title, index / len(MOVIES))
            movies.append(
                {
                    "movieid": movie_id,
                    "label": title,
                    "title": title,
                    "year": year,
                    "file": f"smb://nas/movies/{title}.mkv",
                    "thumbnail": image_path(name),
                    "art": {"poster": image_path(name)},
                }
            )
        self.library["movies"] = movies
        show_id, show_title = SHOW
        self.images["show"] = poster("show", show_title, 0.58)
        self.library["episodes"] = [
            {
                "episodeid": episode_id,
                "label": f"{season}x{number:02d}. {title}",
                "title": title,
                "showtitle": show_title,
                "season": season,
                "episode": number,
                "tvshowid": show_id,
                "seasonid": 41,
                "file": f"smb://nas/shows/{show_title}/S{season:02d}E{number:02d}.mkv",
                "thumbnail": image_path("show"),
                "art": {"poster": image_path("show")},
            }
            for episode_id, season, number, title in EPISODES
        ]
        # The queue: the second episode plays, two more and a movie follow.
        self.playlist = [self._queue_entry("episodes", e) for e in self.library["episodes"][:3]]
        self.playlist.append(self._queue_entry("movies", movies[2]))
        self.player_active = True
        self.position = 1
        self.time = {"hours": 0, "minutes": 17, "seconds": 42, "milliseconds": 0}

    @staticmethod
    def _queue_entry(kind: str, item: dict[str, Any]) -> dict[str, Any]:
        entry = {
            key: item[key]
            for key in ("label", "title", "file", "thumbnail", "showtitle", "season", "episode")
            if key in item
        }
        entry["type"] = "episode" if kind == "episodes" else "movie"
        entry["id"] = item.get("episodeid", item.get("movieid"))
        entry["duration"] = 2640 if kind == "episodes" else 6120
        return entry

    def _to_playlist_item(self, entry: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        for key, kind in (("movieid", "movies"), ("episodeid", "episodes")):
            if key in entry:
                for item in self.library[kind]:
                    if item[key] == entry[key]:
                        return self._queue_entry(kind, item)
        return FakeKodi._to_playlist_item(entry)

    # Calls of the built-in Kodi integration --------------------------------------

    def _JSONRPC_Version(self) -> dict[str, Any]:
        return {"version": {"major": 13, "minor": 5, "patch": 0}}

    def _Application_GetProperties(self, properties: list[str]) -> dict[str, Any]:
        values = {
            "volume": 62,
            "muted": False,
            "name": "Kodi",
            "version": {"major": 21, "minor": 2, "tag": "stable"},
        }
        return {key: values[key] for key in properties if key in values}

    def _Player_GetProperties(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        values = super()._Player_GetProperties(playerid, properties)
        item = self.playlist[self.position] if self.position >= 0 else {}
        total = item.get("duration", 2640)
        values.update(
            {
                "totaltime": {
                    "hours": total // 3600,
                    "minutes": total % 3600 // 60,
                    "seconds": total % 60,
                    "milliseconds": 0,
                },
                "speed": 1,
                "live": False,
                "shuffled": False,
                "repeat": "off",
            }
        )
        return values

    def _Player_GetItem(self, playerid: int, properties: list[str]) -> dict[str, Any]:
        result = super()._Player_GetItem(playerid, properties)
        item = result["item"]
        item.setdefault("uniqueid", {})
        item.setdefault("streamdetails", {"video": [{"hdrtype": ""}]})
        return result

    def _Player_PlayPause(self, playerid: int, play: Any = "toggle") -> dict[str, Any]:
        return {"speed": 1}

    def _Application_SetVolume(self, volume: Any) -> int:
        return 62

    def _VideoLibrary_GetTVShows(self, **_: Any) -> dict[str, Any]:
        show_id, title = SHOW
        return {
            "tvshows": [
                {
                    "tvshowid": show_id,
                    "label": title,
                    "thumbnail": image_path("show"),
                    "art": {"poster": image_path("show")},
                }
            ],
            "limits": {"total": 1},
        }

    def _VideoLibrary_GetTVShowDetails(self, tvshowid: int, **_: Any) -> dict[str, Any]:
        return {
            "tvshowdetails": {
                "tvshowid": tvshowid,
                "label": SHOW[1],
                "thumbnail": image_path("show"),
                "art": {"poster": image_path("show")},
            }
        }

    def _VideoLibrary_GetSeasons(self, **_: Any) -> dict[str, Any]:
        return {
            "seasons": [
                {
                    "seasonid": 41,
                    "label": "Season 1",
                    "season": 1,
                    "tvshowid": SHOW[0],
                    "thumbnail": image_path("show"),
                    "art": {"poster": image_path("show")},
                }
            ],
            "limits": {"total": 1},
        }

    def _VideoLibrary_GetSeasonDetails(self, seasonid: int, **_: Any) -> dict[str, Any]:
        return {
            "seasondetails": {
                "seasonid": seasonid,
                "label": "Season 1",
                "season": 1,
                "tvshowid": SHOW[0],
                "thumbnail": image_path("show"),
                "art": {"poster": image_path("show")},
            }
        }

    def _VideoLibrary_GetMovieDetails(self, movieid: int, **_: Any) -> dict[str, Any]:
        for movie in self.library["movies"]:
            if movie["movieid"] == movieid:
                return {"moviedetails": movie}
        raise KodiRpcError("VideoLibrary.GetMovieDetails", -32602, "Invalid params.")

    def _AudioLibrary_GetArtists(self, **_: Any) -> dict[str, Any]:
        return {"artists": [], "limits": {"total": 0}}

    def _AudioLibrary_GetAlbums(self, **_: Any) -> dict[str, Any]:
        return {"albums": [], "limits": {"total": 0}}

    def _PVR_GetChannels(self, **_: Any) -> dict[str, Any]:
        return {"channels": [], "limits": {"total": 0}}


async def answer(kodi: DemoKodi, request: dict[str, Any]) -> dict[str, Any] | None:
    method, params, request_id = request.get("method"), request.get("params"), request.get("id")
    if method == "Player.GetActivePlayers" and isinstance(params, list | type(None)):
        # Like a real Kodi on the network, answer the Kodi integration a bit
        # later: it writes its first state before the player's properties arrive.
        await asyncio.sleep(0.5)
    try:
        if isinstance(params, list):  # positional parameters, as pykodi sends them
            handler = getattr(kodi, "_" + str(method).replace(".", "_"), None)
            if handler is None:
                raise KodiRpcError(method, -32601, "Method not found.")
            kodi.calls.append((method, {"args": params}))
            result = handler(*params)
        else:
            result = await kodi.call(method, params)
    except KodiRpcError as err:
        _LOGGER.warning("%s: %s", method, err.message)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": err.code or -32601, "message": err.message},
        }
    except (KodiError, TypeError) as err:
        _LOGGER.warning("%s(%s) failed: %s", method, params, err)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": str(err)},
        }
    else:
        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    return None if request_id is None else response


def make_app(kodi: DemoKodi, *, websocket: bool) -> web.Application:
    async def jsonrpc(request: web.Request) -> web.StreamResponse:
        if websocket or request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)

            async def reply(data: str) -> None:
                if (response := await answer(kodi, json.loads(data))) is not None:
                    await ws.send_json(response)

            tasks = set()
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    _LOGGER.debug("ws <- %s", message.data)
                    # Concurrently, like Kodi: a slow answer doesn't hold up the others.
                    task = asyncio.create_task(reply(message.data))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            return ws
        body = await request.json()
        if isinstance(body, list):
            return web.json_response([r for b in body if (r := await answer(kodi, b))])
        return web.json_response(await answer(kodi, body) or {})

    async def image(request: web.Request) -> web.Response:
        path = unquote(unquote(request.match_info["path"]))
        name = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".png")
        if (data := kodi.images.get(name)) is None:
            raise web.HTTPNotFound
        return web.Response(body=data, content_type="image/png")

    app = web.Application()
    app.router.add_route("*", "/jsonrpc", jsonrpc)
    app.router.add_get("/image/{path:.*}", image)
    return app


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ws-port", type=int, default=9090)
    parser.add_argument("-v", action="store_true", help="log every WebSocket call")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO)
    kodi = DemoKodi()
    for port, ws in ((args.port, False), (args.ws_port, True)):
        runner = web.AppRunner(make_app(kodi, websocket=ws))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    _LOGGER.info("Fake Kodi on http://127.0.0.1:%s, ws://127.0.0.1:%s", args.port, args.ws_port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
