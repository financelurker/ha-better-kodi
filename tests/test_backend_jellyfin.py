"""Tests for Jellyfin items from Home Assistant's media browser."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.better_kodi.backends import BackendContext
from custom_components.better_kodi.backends.jellyfin import (
    JellyfinBackend,
    JellyfinItem,
    JellyfinStream,
    async_find_in_kodi_library,
    async_get_item,
    display_title,
    parse_stream_url,
)
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
)
from homeassistant.components.media_source import PlayMedia
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

JF_ID = "0123456789abcdef0123456789abcdef"
SERVER = "http://jelly.local:8096"
STREAM = f"{SERVER}/Videos/{JF_ID}/stream?static=true&DeviceId=ha&api_key=KEY"
EPISODE = {
    "Name": "Pilot",
    "Type": "Episode",
    "SeriesName": "The Show",
    "ParentIndexNumber": 1,
    "IndexNumber": 2,
    "Path": "/media/shows/The Show/S01E02 Pilot.mkv",
}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (STREAM, JellyfinStream(SERVER, JF_ID, "KEY")),
        (
            f"https://media.example/jellyfin/Videos/{JF_ID.upper()}/stream.mkv?api_key=K",
            JellyfinStream("https://media.example/jellyfin", JF_ID, "K"),
        ),
        (
            f"{SERVER}/Videos/{JF_ID}/stream?static=true&ApiKey=NEW",
            JellyfinStream(SERVER, JF_ID, "NEW"),
        ),
        (f"{SERVER}/Audio/{JF_ID}/universal?api_key=K", None),
        ("https://cdn.example/stream", None),
    ],
)
def test_parse_stream_url(url: str, expected: JellyfinStream | None) -> None:
    assert parse_stream_url(url) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (EPISODE, "The Show – S01E02 – Pilot"),
        ({"Name": "Film", "Type": "Movie", "ProductionYear": 2024}, "Film (2024)"),
        ({"Name": "Clip", "Type": "Video"}, "Clip"),
        ({"Name": "Odd", "Type": "Episode", "SeriesName": "S", "IndexNumber": "x"}, "S – Odd"),
    ],
)
def test_display_title(item: dict[str, Any], expected: str) -> None:
    assert display_title(item) == expected


async def test_get_item(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{SERVER}/Items", json={"Items": [EPISODE]})
    item = await async_get_item(async_get_clientsession(hass), parse_stream_url(STREAM))
    assert item == JellyfinItem(
        item_id=JF_ID,
        kind="Episode",
        title="The Show – S01E02 – Pilot",
        thumbnail=f"{SERVER}/Items/{JF_ID}/Images/Primary?maxWidth=400",
        path=EPISODE["Path"],
    )
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert dict(url.query) == {"ids": JF_ID, "fields": "Path"}  # no key in the URL
    assert headers["Authorization"] == 'MediaBrowser Token="KEY"'


async def test_get_item_failure(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{SERVER}/Items", status=401)
    assert await async_get_item(async_get_clientsession(hass), parse_stream_url(STREAM)) is None


async def test_lookup_title_of_stream(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{SERVER}/Items", json={"Items": [EPISODE]})
    backend = JellyfinBackend()
    context = BackendContext(async_get_clientsession(hass), FakeKodi(), _no_addons)  # type: ignore[arg-type]
    assert backend.recognizes(STREAM)
    title, _ = await backend.async_title(context, STREAM)
    assert title == "The Show – S01E02 – Pilot"


async def _no_addons() -> frozenset[str]:
    return frozenset()


def item(**changes: Any) -> JellyfinItem:
    return JellyfinItem(
        **{
            "item_id": JF_ID,
            "kind": "Episode",
            "title": "t",
            "thumbnail": "",
            "path": None,
            **changes,
        }
    )


async def test_find_synced_item_by_id() -> None:
    kodi = FakeKodi()
    kodi.library["episodes"] = [
        {
            "episodeid": 7,
            "file": f"plugin://plugin.video.jellyfin/lib/?filename=a.mkv&id={JF_ID}&dbid=7&mode=play",
        },
        {
            "episodeid": 8,
            "file": "plugin://plugin.video.jellyfin/lib/?filename=b.mkv&id=ffff&dbid=8&mode=play",
        },
    ]
    assert await async_find_in_kodi_library(kodi, item()) == {"episodeid": 7}  # type: ignore[arg-type]


async def test_find_direct_path_item_by_file_name() -> None:
    kodi = FakeKodi()
    kodi.library["episodes"] = [
        {"episodeid": 9, "file": "smb://nas/shows/The Show/S01E02 Pilot.mkv"}
    ]
    found = await async_find_in_kodi_library(kodi, item(path=EPISODE["Path"]))  # type: ignore[arg-type]
    assert found == {"episodeid": 9}


async def test_same_file_name_in_another_show_is_not_used() -> None:
    """Kodi only compares file names; "S01E01.mkv" exists in many shows."""
    kodi = FakeKodi()
    kodi.library["episodes"] = [{"episodeid": 3, "file": "smb://nas/shows/Other Show/S01E01.mkv"}]
    found = await async_find_in_kodi_library(  # type: ignore[arg-type]
        kodi, item(path="/media/shows/The Show/S01E01.mkv")
    )
    assert found is None


async def test_music_video() -> None:
    kodi = FakeKodi()
    kodi.library["musicvideos"] = [
        {
            "musicvideoid": 4,
            "file": f"plugin://plugin.video.jellyfin/l/?filename=v.mp4&id={JF_ID}&mode=play",
        }
    ]
    found = await async_find_in_kodi_library(kodi, item(kind="MusicVideo"))  # type: ignore[arg-type]
    assert found == {"musicvideoid": 4}


async def test_ambiguous_file_name_is_not_used() -> None:
    kodi = FakeKodi()
    kodi.library["movies"] = [
        {"movieid": 1, "file": "smb://a/x/movie.mkv"},
        {"movieid": 2, "file": "smb://b/x/movie.mkv"},
    ]
    found = await async_find_in_kodi_library(kodi, item(kind="Movie", path="/x/movie.mkv"))  # type: ignore[arg-type]
    assert found is None


async def test_unknown_kind_is_not_searched() -> None:
    assert await async_find_in_kodi_library(FakeKodi(), item(kind="Audio")) is None  # type: ignore[arg-type]


@pytest.fixture
async def setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
) -> tuple[str, FakeKodi]:
    aioclient_mock.get(f"{SERVER}/Items", json={"Items": [EPISODE]})
    entry = await setup_better_kodi(hass, kodi_source)
    return own_entity_id(hass, entry), fakes[kodi_source.host]


async def play_jellyfin(hass: HomeAssistant, player: str, enqueue: str = "play") -> None:
    with patch(
        "custom_components.better_kodi.media_player.media_source.async_resolve_media",
        return_value=PlayMedia(STREAM, "video/x-matroska"),
    ):
        await hass.services.async_call(
            MP_DOMAIN,
            "play_media",
            {
                ATTR_ENTITY_ID: player,
                ATTR_MEDIA_CONTENT_ID: f"media-source://jellyfin/{JF_ID}",
                ATTR_MEDIA_CONTENT_TYPE: "video/x-matroska",
                "enqueue": enqueue,
            },
            blocking=True,
        )


async def test_media_browser_item_synced_to_kodi(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    player, kodi = setup
    kodi.library["episodes"] = [
        {
            "episodeid": 7,
            "file": f"plugin://plugin.video.jellyfin/lib/?filename=a.mkv&id={JF_ID}&dbid=7&mode=play",
        }
    ]
    await play_jellyfin(hass, player)
    assert kodi.playlist[0]["id"] == 7
    assert kodi.playlist[0]["type"] == "episode"


async def test_media_browser_item_through_addon(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    player, kodi = setup
    kodi.addons = {"plugin.video.jellyfin"}
    entry = hass.config_entries.async_entries("better_kodi")[0]
    await entry.runtime_data.async_get_addons(force=True)
    await play_jellyfin(hass, player)
    assert kodi.files() == [f"plugin://plugin.video.jellyfin/?mode=play&id={JF_ID}"]
    queue = await hass.services.async_call(
        "better_kodi", "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert queue[player]["items"][0]["title"] == "The Show – S01E02 – Pilot"


async def test_media_browser_item_as_stream(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    player, kodi = setup
    await play_jellyfin(hass, player)
    await play_jellyfin(hass, player, enqueue="add")
    assert kodi.files() == [STREAM, STREAM]
    queue = await hass.services.async_call(
        "better_kodi", "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert [entry["title"] for entry in queue[player]["items"]] == ["The Show – S01E02 – Pilot"] * 2


DOWNLOAD = f"{SERVER}/Items/{JF_ID}/Download?api_key=KEY"


@pytest.mark.parametrize(
    "url",
    [
        DOWNLOAD,
        f"{SERVER}/Items/01234567-89ab-cdef-0123-456789abcdef/Download?api_key=KEY",
        f"{SERVER}/Videos/{JF_ID}/master.m3u8?ApiKey=KEY",
    ],
)
def test_parse_download_and_hls_links(url: str) -> None:
    assert parse_stream_url(url) == JellyfinStream(SERVER, JF_ID, "KEY")


async def share_link(hass: HomeAssistant, player: str, url: str) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: url,
            ATTR_MEDIA_CONTENT_TYPE: "url",
            "enqueue": "add",
        },
        blocking=True,
    )


async def test_copied_stream_link_plays_the_library_item(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    """A link from Jellyfin's "Copy stream URL", shared from the phone."""
    player, kodi = setup
    kodi.library["episodes"] = [
        {
            "episodeid": 7,
            "file": f"plugin://plugin.video.jellyfin/lib/?filename=a.mkv&id={JF_ID}&dbid=7&mode=play",
        }
    ]
    await share_link(hass, player, DOWNLOAD)
    assert kodi.playlist[0]["id"] == 7
    assert kodi.playlist[0]["type"] == "episode"


async def test_copied_stream_link_through_addon(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    player, kodi = setup
    kodi.addons = {"plugin.video.jellyfin"}
    await hass.config_entries.async_entries("better_kodi")[0].runtime_data.async_get_addons(
        force=True
    )
    await share_link(hass, player, DOWNLOAD)
    assert kodi.files() == [f"plugin://plugin.video.jellyfin/?mode=play&id={JF_ID}"]
    queue = await hass.services.async_call(
        "better_kodi", "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert queue[player]["items"][0]["title"] == "The Show – S01E02 – Pilot"


async def test_download_entry_queued_elsewhere_gets_its_title(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    player, kodi = setup
    kodi.playlist.append({"file": DOWNLOAD, "label": "Download"})
    queue = await hass.services.async_call(
        "better_kodi", "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert queue[player]["items"][0]["title"] == "The Show – S01E02 – Pilot"


async def test_copied_link_without_jellyfin_answer_uses_the_addon(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
) -> None:
    """Home Assistant can't reach jellyfin.local (or the key is refused): the ID is enough."""
    aioclient_mock.get(f"{SERVER}/Items", status=401)
    kodi = fakes[kodi_source.host]
    kodi.addons = {"plugin.video.jellyfin"}
    entry = await setup_better_kodi(hass, kodi_source)
    await share_link(hass, own_entity_id(hass, entry), DOWNLOAD)
    assert kodi.files() == [f"plugin://plugin.video.jellyfin/?mode=play&id={JF_ID}"]


async def test_copied_link_without_jellyfin_answer_uses_the_library(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
) -> None:
    aioclient_mock.get(f"{SERVER}/Items", exc=TimeoutError)
    kodi = fakes[kodi_source.host]
    kodi.library["movies"] = [
        {"movieid": 3, "file": f"plugin://plugin.video.jellyfin/m/?filename=x.mkv&id={JF_ID}"}
    ]
    entry = await setup_better_kodi(hass, kodi_source)
    await share_link(hass, own_entity_id(hass, entry), DOWNLOAD)
    assert kodi.playlist[0]["id"] == 3


async def test_download_link_without_addon_becomes_a_stream(
    hass: HomeAssistant, setup: tuple[str, FakeKodi]
) -> None:
    """Kodi can't play "Download" links with api_key; the stream of the item works."""
    player, kodi = setup
    await share_link(hass, player, DOWNLOAD)
    assert kodi.files() == [f"{SERVER}/Videos/{JF_ID}/stream?static=true&api_key=KEY&ApiKey=KEY"]
    queue = await hass.services.async_call(
        "better_kodi", "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert queue[player]["items"][0]["title"] == "The Show – S01E02 – Pilot"


async def test_folders_are_not_turned_into_play_paths(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
) -> None:
    aioclient_mock.get(f"{SERVER}/Items", json={"Items": [{"Name": "Show", "Type": "Series"}]})
    kodi = fakes[kodi_source.host]
    kodi.addons = {"plugin.video.jellyfin"}
    entry = await setup_better_kodi(hass, kodi_source)
    await share_link(hass, own_entity_id(hass, entry), DOWNLOAD)
    assert kodi.files() == [DOWNLOAD]  # left to the link resolvers, unchanged
