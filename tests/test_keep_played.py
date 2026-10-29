"""Tests for the number entity that removes played entries from the queue."""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import PropertyMock, patch

from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.better_kodi.announce import Announcer
from custom_components.better_kodi.const import DOMAIN, KEEP_PLAYED_KEY, MAX_KEEP_PLAYED
from custom_components.better_kodi.kodi_client import KodiClient
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, better_kodi_entry, setup_better_kodi
from .fake_kodi import FakeKodi

FILES = [f"plugin://x/{name}" for name in "abcdef"]


def number_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        NUMBER_DOMAIN, DOMAIN, f"{entry.entry_id}_{KEEP_PLAYED_KEY}"
    )
    assert entity_id is not None
    return entity_id


async def set_keep(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


def play_queue(kodi: FakeKodi, position: int, count: int = 6) -> None:
    """Kodi plays the queue a…f at a position."""
    kodi.playlist = [{"file": file, "label": file, "type": "unknown"} for file in FILES[:count]]
    kodi.current_playlist = 1
    kodi.player_active, kodi.player_type, kodi.position = True, "video", position


def names(kodi: FakeKodi) -> list[str]:
    return [file.rsplit("/", 1)[1] for file in kodi.files() if file]


async def test_entity_defaults_to_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    entity_id = number_id(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == MAX_KEEP_PLAYED
    assert er.async_get(hass).async_get(entity_id).entity_category is EntityCategory.CONFIG
    assert len(kodi.playlist) == 6
    assert "Playlist.Remove" not in kodi.method_calls()


async def test_setting_a_value_tidies_up_at_once(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    entity_id = number_id(hass, entry)

    await set_keep(hass, entity_id, 2)
    assert hass.states.get(entity_id).state == "2"
    assert names(kodi) == ["c", "d", "e", "f"]
    assert kodi.position == 2


async def test_next_item_from_kodi_notification(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 1)

    async def run_notifications(self: KodiClient, callback: Callable[..., None]) -> None:
        kodi.notify = callback
        await asyncio.Event().wait()  # until the entry is unloaded

    with patch.object(KodiClient, "run_notifications", run_notifications):
        entry = await setup_better_kodi(hass, kodi_source)
        await set_keep(hass, number_id(hass, entry), 1)
        assert len(kodi.playlist) == 6  # one played entry: nothing to remove yet

        for expected in (["b", "c", "d", "e", "f"], ["c", "d", "e", "f"]):
            kodi.video_ends()  # Kodi starts the next entry and sends Player.OnPlay
            await hass.async_block_till_done()
            assert names(kodi) == expected
            assert kodi.position == 1
        await hass.config_entries.async_unload(entry.entry_id)


async def test_next_item_without_notifications(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Without Kodi's WebSocket, the Kodi entity's next item triggers it."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, number_id(hass, entry), 1)

    kodi.position = 3  # Kodi went on by itself
    state = hass.states.get(kodi_source.entity_id)
    hass.states.async_set(
        kodi_source.entity_id, state.state, {**state.attributes, "media_title": "d"}
    )
    await hass.async_block_till_done()
    assert names(kodi) == ["c", "d", "e", "f"]
    assert kodi.position == 1


async def test_value_survives_a_restart(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 5)
    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    # The number entity's ID is known before setup from its registry entry.
    registry_entry = er.async_get(hass).async_get_or_create(
        NUMBER_DOMAIN,
        DOMAIN,
        f"{entry.entry_id}_{KEEP_PLAYED_KEY}",
        config_entry=entry,
        suggested_object_id="wohnzimmer_kodi_keep_played",
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(registry_entry.entity_id, "3"),
                {
                    "native_max_value": MAX_KEEP_PLAYED,
                    "native_min_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": None,
                    "native_value": 3,
                },
            )
        ],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(registry_entry.entity_id).state == "3"
    assert names(kodi) == ["c", "d", "e", "f"]  # tidied up right after the start


async def test_not_while_an_item_reopened_after_an_announcement_waits(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """The entries in front of such an item are the noted queue, not played ones."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    with patch.object(
        Announcer, "watching_reopened_item", new_callable=PropertyMock, return_value=True
    ):
        await set_keep(hass, number_id(hass, entry), 0)
        await set_keep(hass, number_id(hass, entry), 1)
    assert len(kodi.playlist) == 6
    assert "Playlist.Remove" not in kodi.method_calls()


async def test_kodi_errors_are_not_raised(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    kodi.offline = True
    await set_keep(hass, number_id(hass, entry), 1)  # no error for the user
    kodi.offline = False
    assert hass.states.get(number_id(hass, entry)).state == "1"
    assert len(kodi.playlist) == 6


async def test_quick_item_changes_are_combined(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, number_id(hass, entry), 1)
    data: Any = entry.runtime_data

    kodi.position = 4
    for _ in range(5):
        data.schedule_trim()
    await hass.async_block_till_done()
    assert names(kodi) == ["d", "e", "f"]
    # One run plus one combined run afterwards, each reading the status once
    assert kodi.method_calls().count("Playlist.Remove") == 3
