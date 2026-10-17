"""Tests for the Better Kodi media player."""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.better_kodi.const import (
    DOMAIN,
    QUEUE_CONTENT_ID,
    QUEUE_CONTENT_TYPE,
)
from custom_components.better_kodi.media_player import BetterKodiMediaPlayer, extract_link
from custom_components.better_kodi.resolvers import ADDON_JELLYCON, ADDON_YOUTUBE, YOUTUBE_OEMBED
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DATA_COMPONENT,
    DOMAIN as MP_DOMAIN,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import KodiSource, SourceFactory, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

VIDEO_ID = "dQw4w9WgXcQ"


def entity_of(hass: HomeAssistant, entity_id: str) -> BetterKodiMediaPlayer:
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert isinstance(entity, BetterKodiMediaPlayer)
    return entity


async def play_media(
    hass: HomeAssistant, entity_id: str, media_id: str, media_type: str = "url", **extra: Any
) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: entity_id,
            ATTR_MEDIA_CONTENT_ID: media_id,
            ATTR_MEDIA_CONTENT_TYPE: media_type,
            **extra,
        },
        blocking=True,
    )


@pytest.fixture
async def player(hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]) -> str:
    """Set up Better Kodi and return its entity ID."""
    fakes[kodi_source.host].addons = {ADDON_YOUTUBE}
    entry = await setup_better_kodi(hass, kodi_source)
    return own_entity_id(hass, entry)


async def test_mirrors_source_and_joins_its_device(
    hass: HomeAssistant, kodi_source: KodiSource, player: str
) -> None:
    state = hass.states.get(player)
    assert state.state == "playing"
    assert state.name == "Wohnzimmer Kodi"
    assert state.attributes["volume_level"] == 0.5
    assert state.attributes["media_title"] == "Something"
    assert state.attributes["entity_picture"].startswith(
        f"/api/media_player_proxy/{kodi_source.entity_id}"
    )
    assert state.attributes["dynamic_range"] == "hdr10"
    features = MediaPlayerEntityFeature(state.attributes["supported_features"])
    assert MediaPlayerEntityFeature.PAUSE in features
    assert MediaPlayerEntityFeature.MEDIA_ENQUEUE in features
    assert MediaPlayerEntityFeature.CLEAR_PLAYLIST in features

    entity = er.async_get(hass).async_get(player)
    assert entity.device_id == kodi_source.device_id
    assert entity.platform == DOMAIN


async def test_follows_source_state(
    hass: HomeAssistant, kodi_source: KodiSource, player: str
) -> None:
    hass.states.async_set(kodi_source.entity_id, "paused", {"media_title": "Other"})
    await hass.async_block_till_done()
    state = hass.states.get(player)
    assert state.state == "paused"
    assert state.attributes["media_title"] == "Other"

    hass.states.async_set(kodi_source.entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(player).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("method", "args", "service", "data"),
    [
        ("async_media_pause", (), "media_pause", {}),
        ("async_media_play", (), "media_play", {}),
        ("async_media_stop", (), "media_stop", {}),
        ("async_turn_on", (), "turn_on", {}),
        ("async_turn_off", (), "turn_off", {}),
        ("async_set_volume_level", (0.3,), "volume_set", {"volume_level": 0.3}),
        ("async_mute_volume", (True,), "volume_mute", {"is_volume_muted": True}),
        ("async_media_seek", (42.0,), "media_seek", {"seek_position": 42.0}),
        ("async_media_next_track", (), "media_next_track", {}),
    ],
)
async def test_forwards_commands_to_kodi_entity(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    player: str,
    method: str,
    args: tuple,
    service: str,
    data: dict,
) -> None:
    calls = async_mock_service(hass, MP_DOMAIN, service)
    await getattr(entity_of(hass, player), method)(*args)
    assert len(calls) == 1
    assert calls[0].data == {ATTR_ENTITY_ID: kodi_source.entity_id, **data}


async def test_play_youtube_link_with_title(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
    player: str,
) -> None:
    aioclient_mock.get(
        YOUTUBE_OEMBED, json={"title": "Never Gonna", "thumbnail_url": "https://i/x.jpg"}
    )
    kodi = fakes[kodi_source.host]
    await play_media(hass, player, f"https://youtu.be/{VIDEO_ID}")
    await hass.async_block_till_done()

    assert kodi.files() == [f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}"]
    assert kodi.position == 0
    state = hass.states.get(player)
    assert state.attributes["queue_length"] == 1
    assert state.attributes["queue_position"] == 0

    queue = await hass.services.async_call(
        DOMAIN, "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert queue[player]["items"][0]["title"] == "Never Gonna"
    assert queue[player]["items"][0]["current"] is True


async def test_enqueue_next_and_add(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    kodi = fakes[kodi_source.host]
    await play_media(hass, player, "https://cdn.example/a.mp4")
    await play_media(hass, player, "https://cdn.example/b.mp4", enqueue="add")
    await play_media(hass, player, "https://cdn.example/c.mp4", enqueue="next")
    assert kodi.files() == [
        "https://cdn.example/a.mp4",
        "https://cdn.example/c.mp4",
        "https://cdn.example/b.mp4",
    ]
    assert kodi.position == 0


async def test_library_items_by_id(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    kodi = fakes[kodi_source.host]
    await play_media(hass, player, "https://cdn.example/a.mp4")
    await play_media(hass, player, "17", "movie", enqueue="add")
    assert kodi.playlist[-1]["id"] == 17
    assert kodi.playlist[-1]["type"] == "movie"


async def test_play_only_link_cannot_be_queued(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    fakes[kodi_source.host].addons = {ADDON_JELLYCON}
    entity = entity_of(hass, player)
    await entity._data.async_get_addons(force=True)
    with pytest.raises(ServiceValidationError) as err:
        await play_media(
            hass,
            player,
            "https://jelly/web/#/details?id=0123456789abcdef0123456789abcdef",
            enqueue="next",
        )
    assert err.value.translation_key == "play_only"


async def test_other_library_types_are_forwarded(
    hass: HomeAssistant, kodi_source: KodiSource, player: str
) -> None:
    calls = async_mock_service(hass, MP_DOMAIN, "play_media")
    entity = entity_of(hass, player)
    await entity.async_play_media("tvshow", "5")
    assert calls[0].data[ATTR_ENTITY_ID] == kodi_source.entity_id
    assert calls[0].data[ATTR_MEDIA_CONTENT_ID] == "5"

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_play_media("tvshow", "5", enqueue="add")
    assert err.value.translation_key == "enqueue_not_supported"


async def test_media_source_items_are_queued(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    hass.config.internal_url = "http://ha.local:8123"
    await play_media(
        hass, player, "media-source://media_source/local/clip.mp4", "video", enqueue="replace"
    )
    file = fakes[kodi_source.host].files()[0]
    assert file.startswith("http://ha.local:8123/media/local/clip.mp4?authSig=")


async def test_clear_playlist(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    await play_media(hass, player, "https://cdn.example/a.mp4")
    await hass.services.async_call(
        MP_DOMAIN, "clear_playlist", {ATTR_ENTITY_ID: player}, blocking=True
    )
    assert fakes[kodi_source.host].playlist == []


async def test_browse_root_adds_queue_folder(
    hass: HomeAssistant, kodi_source: KodiSource, player: str
) -> None:
    calls = async_mock_service(
        hass,
        MP_DOMAIN,
        "browse_media",
        response={
            kodi_source.entity_id: {
                "title": "Kodi",
                "media_class": "directory",
                "media_content_id": "",
                "media_content_type": "library",
                "can_play": False,
                "can_expand": True,
                "children": [
                    {
                        "title": "Movies",
                        "media_class": "directory",
                        "media_content_id": "",
                        "media_content_type": "library_movies",
                        "can_play": False,
                        "can_expand": True,
                    }
                ],
            }
        },
        supports_response=SupportsResponse.ONLY,
    )
    result = await entity_of(hass, player).async_browse_media()
    assert [child.title for child in result.children] == ["Queue", "Movies"]
    assert result.children[0].media_content_type == QUEUE_CONTENT_TYPE
    assert calls[0].data == {ATTR_ENTITY_ID: kodi_source.entity_id}

    await entity_of(hass, player).async_browse_media("library_movies", "")
    assert calls[1].data[ATTR_MEDIA_CONTENT_TYPE] == "library_movies"


async def test_browse_queue_and_jump(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    kodi = fakes[kodi_source.host]
    await play_media(hass, player, "https://cdn.example/a.mp4")
    await play_media(hass, player, "https://cdn.example/b.mp4", enqueue="add")
    kodi.playlist[1]["thumbnail"] = "image://smb%3a%2f%2fnas%2fb.jpg/"

    folder = await entity_of(hass, player).async_browse_media(QUEUE_CONTENT_TYPE, QUEUE_CONTENT_ID)
    assert [child.title for child in folder.children] == [
        "▶ https://cdn.example/a.mp4",
        "https://cdn.example/b.mp4",
    ]
    assert folder.children[1].thumbnail.startswith(
        f"/api/media_player_proxy/{player}/browse_media/"
    )

    await play_media(hass, player, folder.children[1].media_content_id, "better_kodi_queue_item")
    assert kodi.position == 1


async def test_queue_services(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    kodi = fakes[kodi_source.host]
    for name in ("a", "b", "c"):
        await play_media(hass, player, f"https://cdn.example/{name}.mp4", enqueue="add")
    await hass.services.async_call(
        DOMAIN,
        "move_queue_item",
        {ATTR_ENTITY_ID: player, "position": 2, "to_position": 1},
        blocking=True,
    )
    assert [f.rsplit("/", 1)[1] for f in kodi.files()] == ["a.mp4", "c.mp4", "b.mp4"]

    await hass.services.async_call(
        DOMAIN, "remove_from_queue", {ATTR_ENTITY_ID: player, "position": 2}, blocking=True
    )
    assert len(kodi.playlist) == 2

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "remove_from_queue", {ATTR_ENTITY_ID: player, "position": 0}, blocking=True
        )
    assert err.value.translation_key == "queue_error"


async def test_call_method_returns_result(hass: HomeAssistant, player: str) -> None:
    response = await hass.services.async_call(
        DOMAIN,
        "call_method",
        {
            ATTR_ENTITY_ID: player,
            "method": "Playlist.GetProperties",
            "params": {"playlistid": 1, "properties": ["size"]},
        },
        blocking=True,
        return_response=True,
    )
    assert response[player] == {"result": {"size": 0, "type": "video"}}


async def test_handover(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    make_kodi_source: SourceFactory,
    fakes: dict[str, FakeKodi],
    player: str,
) -> None:
    other_source = await make_kodi_source("Schlafzimmer Kodi", "10.0.0.6")
    other_entry = await setup_better_kodi(hass, other_source)
    other_player = own_entity_id(hass, other_entry)
    stop_calls = async_mock_service(hass, MP_DOMAIN, "media_stop")

    source_kodi, target_kodi = fakes[kodi_source.host], fakes[other_source.host]
    for name in ("a", "b", "c"):
        await play_media(hass, player, f"https://cdn.example/{name}.mp4", enqueue="add")
    await source_kodi.call("Player.GoTo", {"playerid": 1, "to": 1})
    source_kodi.time = {"hours": 0, "minutes": 3, "seconds": 7, "milliseconds": 0}

    await hass.services.async_call(
        DOMAIN, "handover", {ATTR_ENTITY_ID: player, "target": other_player}, blocking=True
    )
    assert target_kodi.files() == source_kodi.files()
    assert target_kodi.position == 1
    assert target_kodi.seeks == [{"time": source_kodi.time}]
    assert stop_calls[0].data == {ATTR_ENTITY_ID: kodi_source.entity_id}


async def test_handover_to_itself_is_refused(hass: HomeAssistant, player: str) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "handover", {ATTR_ENTITY_ID: player, "target": player}, blocking=True
        )
    assert err.value.translation_key == "invalid_target"


async def test_assist_settings_move_and_come_back(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    exposed_entities.async_expose_entity(hass, "conversation", kodi_source.entity_id, True)
    er.async_get(hass).async_update_entity(kodi_source.entity_id, aliases=["Fernseher"])
    er.async_get(hass).async_update_entity(
        kodi_source.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
    )

    entry = await setup_better_kodi(hass, kodi_source)
    player = own_entity_id(hass, entry)
    assert exposed_entities.async_should_expose(hass, "conversation", player)
    assert not exposed_entities.async_should_expose(hass, "conversation", kodi_source.entity_id)
    assert "Fernseher" in er.async_get(hass).async_get(player).aliases

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    source = er.async_get(hass).async_get(kodi_source.entity_id)
    assert source.hidden_by is None
    assert exposed_entities.async_should_expose(hass, "conversation", kodi_source.entity_id)


async def test_not_ready_while_kodi_is_offline(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    fakes[kodi_source.host].offline = True
    from .conftest import better_kodi_entry

    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_queue_attributes_follow_notifications(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    kodi = fakes[kodi_source.host]
    await kodi.call("Playlist.Add", {"playlistid": 1, "item": [{"file": "x"}, {"file": "y"}]})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry.runtime_data.notify_queue_changed()
    await hass.async_block_till_done()
    assert hass.states.get(player).attributes["queue_length"] == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://youtu.be/abc", "https://youtu.be/abc"),
        ("  https://youtu.be/abc  ", "https://youtu.be/abc"),
        ("Schau dir das an: https://youtu.be/abc?si=x\nVia YouTube", "https://youtu.be/abc?si=x"),
        ("no link here", "no link here"),
    ],
)
def test_extract_link(text: str, expected: str) -> None:
    assert extract_link(text) == expected


async def test_play_link_from_shared_text(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi], player: str
) -> None:
    await play_media(hass, player, "Look at this https://cdn.example/a.mp4 !")
    assert fakes[kodi_source.host].files() == ["https://cdn.example/a.mp4"]
