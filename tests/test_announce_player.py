"""Announcements through Home Assistant: play_media with announce, tts.speak."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.better_kodi import announce
from custom_components.better_kodi.const import (
    CONF_ANNOUNCE_VOLUME,
    CONF_POWER_OFF,
    DEFAULT_OPTIONS,
)
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_source import PlayMedia
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi
from .test_wake import kodi_integration_turn_on, wake_on_lan_automation

CLIP = "http://ha.local:8123/api/tts_proxy/abc.mp3"
MOVIE = "smb://nas/movies/a.mkv"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announce, "POLL_INTERVAL", 0.05)


@pytest.fixture
async def playing(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> tuple[ConfigEntry, str, FakeKodi]:
    """Better Kodi on a Kodi that plays a movie from the queue."""
    kodi = fakes[kodi_source.host]
    kodi.playlist = [{"file": MOVIE, "label": "a", "type": "unknown"}]
    kodi.player_active, kodi.position = True, 0
    kodi.time = {"hours": 1, "minutes": 2, "seconds": 3, "milliseconds": 0}
    entry = await setup_better_kodi(hass, kodi_source)
    return entry, own_entity_id(hass, entry), kodi


async def announce_media(
    hass: HomeAssistant, player: str, media_id: str = CLIP, **extra: Any
) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: media_id,
            ATTR_MEDIA_CONTENT_TYPE: "music",
            "announce": True,
            **extra,
        },
        blocking=True,
    )


async def test_announce_feature(hass: HomeAssistant, playing) -> None:
    _, player, _ = playing
    features = MediaPlayerEntityFeature(hass.states.get(player).attributes["supported_features"])
    assert MediaPlayerEntityFeature.MEDIA_ANNOUNCE in features


async def test_announcement_interrupts_and_continues(hass: HomeAssistant, playing) -> None:
    _, player, kodi = playing
    await announce_media(hass, player)
    assert kodi.clips == [CLIP]
    assert kodi.files() == [MOVIE]  # not added to the queue
    assert kodi.position == 0
    assert kodi.time["minutes"] == 2


async def test_without_announce_it_plays_as_usual(hass: HomeAssistant, playing) -> None:
    _, player, kodi = playing
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {ATTR_ENTITY_ID: player, ATTR_MEDIA_CONTENT_ID: CLIP, ATTR_MEDIA_CONTENT_TYPE: "music"},
        blocking=True,
    )
    assert kodi.files() == [MOVIE, CLIP]  # "play now": behind the playing entry
    assert kodi.clips == []


async def test_text_to_speech_media_source(hass: HomeAssistant, playing) -> None:
    _, player, kodi = playing
    with patch(
        "custom_components.better_kodi.media_player.media_source.async_resolve_media",
        return_value=PlayMedia(CLIP, "audio/mpeg"),
    ):
        await announce_media(hass, player, "media-source://tts/demo?message=Hallo")
    assert kodi.clips == [CLIP]


async def test_volume_from_options_and_extra(hass: HomeAssistant, playing) -> None:
    entry, player, kodi = playing
    volumes: list[int] = []
    set_volume = kodi._Application_SetVolume
    kodi._Application_SetVolume = lambda volume: volumes.append(volume) or set_volume(volume)
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_ANNOUNCE_VOLUME: 40}
    )
    await hass.async_block_till_done()
    player = own_entity_id(hass, entry)
    await announce_media(hass, player)
    await announce_media(hass, player, extra={"announce_volume": 75})
    assert volumes == [40, 50, 75, 50]


@pytest.mark.parametrize("volume", [120, "loud"])
async def test_invalid_volume(hass: HomeAssistant, playing, volume: Any) -> None:
    _, player, kodi = playing
    with pytest.raises(ServiceValidationError) as err:
        await announce_media(hass, player, extra={"announce_volume": volume})
    assert err.value.translation_key == "announce_volume"
    assert kodi.clips == []


async def test_not_announceable(hass: HomeAssistant, playing) -> None:
    _, player, _ = playing
    with pytest.raises(ServiceValidationError) as err:
        await announce_media(hass, player, "1234")
    assert err.value.translation_key == "announce_not_playable"


async def test_muted_is_not_an_error(hass: HomeAssistant, playing) -> None:
    _, player, kodi = playing
    kodi.muted = True
    await announce_media(hass, player)
    assert kodi.clips == []
    assert kodi.position == 0


async def test_failed_restore_is_reported(hass: HomeAssistant, playing) -> None:
    entry, player, kodi = playing
    kodi.start_delay = 1000  # the movie never starts again
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, "announce_start_timeout": 0.5}
    )
    with pytest.raises(HomeAssistantError) as err:
        await announce_media(hass, player)
    assert err.value.translation_key == "announce_restore_failed"


async def test_commands_wait_for_the_announcement(hass: HomeAssistant, playing) -> None:
    _, player, kodi = playing
    kodi.clip_length = 0.8
    task = hass.async_create_task(announce_media(hass, player))
    await asyncio.sleep(0.2)
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: "https://cdn.example/b.mp4",
            ATTR_MEDIA_CONTENT_TYPE: "url",
            "enqueue": "add",
        },
        blocking=True,
    )
    methods = kodi.method_calls()
    continued = len(methods) - 1 - methods[::-1].index("Player.Open")  # the movie again
    assert methods.index("Playlist.Add") > continued  # added only afterwards
    assert kodi.files() == [MOVIE, "https://cdn.example/b.mp4"]
    await task


@pytest.fixture
async def asleep(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> tuple[ConfigEntry, str, FakeKodi, list[str]]:
    """Kodi is suspended and switches off by suspending again."""
    kodi = fakes[kodi_source.host]
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_POWER_OFF: "suspend"}
    )
    await hass.async_block_till_done()
    kodi.offline = True
    hass.states.async_set(kodi_source.entity_id, "off", {"supported_features": 384})
    events: list[str] = []
    kodi_integration_turn_on(hass, events)
    wake_on_lan_automation(hass, kodi_source, kodi, events)
    return entry, own_entity_id(hass, entry), kodi, events


async def test_woken_for_an_announcement_and_switched_off_again(
    hass: HomeAssistant, asleep
) -> None:
    _, player, kodi, events = asleep
    await announce_media(hass, player)
    assert events == ["turn_on", "automation", "awake"]
    assert kodi.clips == [CLIP]
    assert "System.Suspend" in kodi.method_calls()


async def test_muted_after_waking_switches_off_again(hass: HomeAssistant, asleep) -> None:
    _, player, kodi, _ = asleep
    kodi.muted = True
    await announce_media(hass, player)
    assert kodi.clips == []
    assert "System.Suspend" in kodi.method_calls()


async def test_not_switched_off_when_someone_uses_kodi(hass: HomeAssistant, asleep) -> None:
    _, player, kodi, _ = asleep
    kodi.clip_length = 0.6
    task = hass.async_create_task(announce_media(hass, player))
    await asyncio.sleep(0.4)
    kodi.user_plays(MOVIE)  # someone starts a movie right away
    await task
    assert "System.Suspend" not in kodi.method_calls()
