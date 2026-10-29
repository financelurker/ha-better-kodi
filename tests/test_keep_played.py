"""Tests for the select entity that removes played entries from the queue.

The fake Kodi yields on every call here (like a real connection), so the
background trim really overlaps with other work; tests wait for it explicitly.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import PropertyMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.better_kodi.announce import Announcer
from custom_components.better_kodi.const import DOMAIN, KEEP_PLAYED_KEY, KEEP_PLAYED_OPTIONS
from custom_components.better_kodi.kodi_client import KodiClient
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, better_kodi_entry, setup_better_kodi
from .fake_kodi import FakeKodi

FILES = [f"plugin://x/{name}" for name in "abcdef"]


def select_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        SELECT_DOMAIN, DOMAIN, f"{entry.entry_id}_{KEEP_PLAYED_KEY}"
    )
    assert entity_id is not None
    return entity_id


async def settle(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Wait until Home Assistant and the trim are done."""
    await hass.async_block_till_done()
    await entry.runtime_data.async_wait_trim()
    await hass.async_block_till_done()


async def set_keep(hass: HomeAssistant, entry: MockConfigEntry, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: select_id(hass, entry), ATTR_OPTION: option},
        blocking=True,
    )
    await settle(hass, entry)


def play_queue(kodi: FakeKodi, position: int, count: int = 6) -> None:
    """Kodi plays the queue a…f at a position."""
    kodi.yield_calls = True
    kodi.playlist = [{"file": file, "label": file, "type": "unknown"} for file in FILES[:count]]
    kodi.current_playlist = 1
    kodi.player_active, kodi.player_type, kodi.position = True, "video", position


def names(kodi: FakeKodi) -> list[str]:
    return [file.rsplit("/", 1)[1] for file in kodi.files() if file]


def trim_runs(kodi: FakeKodi) -> int:
    """Count trims that got as far as reading shuffle and repeat."""
    return sum(
        1
        for method, params in kodi.calls
        if method == "Player.GetProperties" and "partymode" in params.get("properties", [])
    )


async def test_entity_defaults_to_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    await settle(hass, entry)
    entity_id = select_id(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes[ATTR_OPTIONS] == list(KEEP_PLAYED_OPTIONS)
    assert er.async_get(hass).async_get(entity_id).entity_category is EntityCategory.CONFIG
    assert len(kodi.playlist) == 6
    assert "Playlist.Remove" not in kodi.method_calls()


async def test_setting_a_value_tidies_up_at_once(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)

    await set_keep(hass, entry, "2")
    assert hass.states.get(select_id(hass, entry)).state == "2"
    assert names(kodi) == ["c", "d", "e", "f"]
    assert kodi.position == 2


async def test_remove_all_played(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 3)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "0")
    assert names(kodi) == ["d", "e", "f"]
    assert kodi.position == 0


async def test_long_queue_is_trimmed_in_batches(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Announcements can get in between batches; the trim still finishes."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    kodi.playlist = [{"file": f"plugin://x/{index}"} for index in range(46)]
    kodi.position = 45
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "1")
    assert len(kodi.playlist) == 2
    assert kodi.position == 1
    assert trim_runs(kodi) == 3  # 20 + 20 + 4


async def test_switched_off_again(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "1")
    await set_keep(hass, entry, "off")
    assert hass.states.get(select_id(hass, entry)).state == "off"

    kodi.position = 4
    entry.runtime_data.schedule_trim()
    await settle(hass, entry)
    assert len(kodi.playlist) == 6


async def test_next_item_from_kodi_notification(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 1)

    async def run_notifications(self: KodiClient, callback: Callable[..., None]) -> None:
        kodi.notify = callback
        self.notifications_connected = True
        await asyncio.Event().wait()  # until the entry is unloaded

    with patch.object(KodiClient, "run_notifications", run_notifications):
        entry = await setup_better_kodi(hass, kodi_source)
        await set_keep(hass, entry, "1")
        assert len(kodi.playlist) == 6  # one played entry: nothing to remove yet

        for expected in (["b", "c", "d", "e", "f"], ["c", "d", "e", "f"]):
            kodi.video_ends()  # Kodi starts the next entry and sends Player.OnPlay
            await settle(hass, entry)
            assert names(kodi) == expected
            assert kodi.position == 1
        await hass.config_entries.async_unload(entry.entry_id)


async def change_source(hass: HomeAssistant, source: KodiSource, state: str, **attrs: Any) -> None:
    current = hass.states.get(source.entity_id)
    hass.states.async_set(source.entity_id, state, {**current.attributes, **attrs})


async def test_next_item_without_notifications(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Without Kodi's WebSocket, the Kodi entity's next item triggers it."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "1")

    kodi.position = 3  # Kodi went on by itself
    await change_source(hass, kodi_source, "playing", media_title="d")
    await settle(hass, entry)
    assert names(kodi) == ["c", "d", "e", "f"]
    assert kodi.position == 1


async def test_pause_does_not_trim(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "1")
    runs = trim_runs(kodi)

    kodi.position = 3
    await change_source(hass, kodi_source, "paused")  # same item, other state
    await settle(hass, entry)
    await change_source(hass, kodi_source, "playing")
    await settle(hass, entry)
    assert trim_runs(kodi) == runs
    assert len(kodi.playlist) == 6


async def test_quick_item_changes_are_combined(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 0)
    entry = await setup_better_kodi(hass, kodi_source)
    await set_keep(hass, entry, "1")
    queue = entry.runtime_data.queue
    runs: list[int] = []
    trim_played = queue.trim_played

    async def counted(keep: int) -> int:
        runs.append(keep)
        return await trim_played(keep)

    kodi.position = 4
    with patch.object(queue, "trim_played", counted):
        for _ in range(5):
            entry.runtime_data.schedule_trim()
        await settle(hass, entry)
    assert names(kodi) == ["d", "e", "f"]
    assert len(runs) == 2  # one run, plus one for the calls meanwhile


async def test_value_survives_a_restart(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 5)
    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    registry_entry = er.async_get(hass).async_get_or_create(
        SELECT_DOMAIN, DOMAIN, f"{entry.entry_id}_{KEEP_PLAYED_KEY}", config_entry=entry
    )
    mock_restore_cache(hass, [State(registry_entry.entity_id, "3")])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass, entry)

    assert hass.states.get(registry_entry.entity_id).state == "3"
    assert names(kodi) == ["c", "d", "e", "f"]  # tidied up right after the start


async def test_unknown_restored_state_is_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 5)
    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    registry_entry = er.async_get(hass).async_get_or_create(
        SELECT_DOMAIN, DOMAIN, f"{entry.entry_id}_{KEEP_PLAYED_KEY}", config_entry=entry
    )
    mock_restore_cache(hass, [State(registry_entry.entity_id, "4")])  # not a choice
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass, entry)
    assert hass.states.get(registry_entry.entity_id).state == "off"
    assert len(kodi.playlist) == 6


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
        await set_keep(hass, entry, "1")
    assert len(kodi.playlist) == 6
    assert "Playlist.Remove" not in kodi.method_calls()


async def test_item_reopened_while_the_trim_waited(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Checked again once it's the trim's turn: an announcement may have ended meanwhile."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    checks = iter([False])  # not before waiting for the turn, but afterwards

    with patch.object(
        Announcer,
        "watching_reopened_item",
        new_callable=PropertyMock,
        side_effect=lambda: next(checks, True),
    ):
        await set_keep(hass, entry, "1")
    assert len(kodi.playlist) == 6
    assert "Playlist.Remove" not in kodi.method_calls()


async def test_kodi_errors_are_not_raised(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    kodi.offline = True
    await set_keep(hass, entry, "1")  # no error for the user
    kodi.offline = False
    assert hass.states.get(select_id(hass, entry)).state == "1"
    assert len(kodi.playlist) == 6


async def test_jump_back_during_the_trim(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Kodi refuses to remove the playing entry; the trim stops without an error."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 4)
    entry = await setup_better_kodi(hass, kodi_source)
    remove = kodi._Playlist_Remove

    def jump_back_first(**params: Any) -> str:
        kodi.position = 0  # someone picks the first entry on the TV
        return remove(**params)

    with patch.object(kodi, "_Playlist_Remove", jump_back_first):
        await set_keep(hass, entry, "1")
    assert len(kodi.playlist) == 6
    assert kodi.position == 0


async def test_unload_during_a_trim(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """The trim is cancelled and leaves no command behind that announcements wait for."""
    kodi = fakes[kodi_source.host]
    play_queue(kodi, 5)
    entry = await setup_better_kodi(hass, kodi_source)
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: select_id(hass, entry), ATTR_OPTION: "0"},
        blocking=True,
    )
    task = entry.runtime_data._trim_task
    assert task is not None
    assert not task.done()
    announcer: Announcer = entry.runtime_data.announcer

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert task.cancelled()
    assert not announcer._commands
    assert announcer._no_commands.is_set()
