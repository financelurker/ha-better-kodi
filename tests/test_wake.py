"""Tests for waking Kodi before sending something to it."""

import asyncio

import pytest

from custom_components.better_kodi.const import CONF_WAKE, CONF_WAKE_TIMEOUT, DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
)
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN, SERVICE_SET_VALUE
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

FILE = "https://cdn.example/a.mp4"


def kodi_integration_turn_on(hass: HomeAssistant, events: list[str]) -> None:
    """Like the Kodi integration: turn_on only fires the kodi.turn_on event."""

    async def turn_on(call: ServiceCall) -> None:
        events.append("turn_on")
        hass.bus.async_fire("kodi.turn_on", {ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID]})

    hass.services.async_register(MP_DOMAIN, "turn_on", turn_on)


def wake_on_lan_automation(
    hass: HomeAssistant, source: KodiSource, kodi: FakeKodi, events: list[str]
):
    """The user's "Kodi turned on" automation: Kodi wakes a moment later."""

    @callback
    def wake() -> None:
        events.append("awake")
        kodi.offline = False
        hass.states.async_set(source.entity_id, "idle", {"supported_features": 384})

    @callback
    def on_event(event) -> None:
        if event.data[ATTR_ENTITY_ID] == source.entity_id:
            events.append("automation")
            hass.loop.call_later(0.05, wake)

    return hass.bus.async_listen("kodi.turn_on", on_event)


@pytest.fixture
async def asleep(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> tuple[ConfigEntry, str, FakeKodi, list[str]]:
    """Kodi is suspended; its turn-on automation wakes it."""
    kodi = fakes[kodi_source.host]
    entry = await setup_better_kodi(hass, kodi_source)
    kodi.offline = True
    hass.states.async_set(kodi_source.entity_id, "off", {"supported_features": 384})
    events: list[str] = []
    kodi_integration_turn_on(hass, events)
    wake_on_lan_automation(hass, kodi_source, kodi, events)
    return entry, own_entity_id(hass, entry), kodi, events


async def play(hass: HomeAssistant, player: str, enqueue: str = "add") -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: FILE,
            ATTR_MEDIA_CONTENT_TYPE: "url",
            "enqueue": enqueue,
        },
        blocking=True,
    )


async def test_wakes_kodi_then_sends(hass: HomeAssistant, asleep) -> None:
    _, player, kodi, events = asleep
    await play(hass, player)
    assert events == ["turn_on", "automation", "awake"]
    assert kodi.files() == [FILE]


async def test_sends_during_one_wake_share_it(hass: HomeAssistant, asleep) -> None:
    """Two buttons pressed while Kodi wakes: the automation runs once, both arrive in order."""
    _, player, kodi, events = asleep
    first = hass.async_create_task(play(hass, player, "add"))
    await asyncio.sleep(0)
    second = hass.async_create_task(
        hass.services.async_call(
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
    )
    await asyncio.gather(first, second)
    assert events.count("turn_on") == 1
    assert kodi.files() == [FILE, "https://cdn.example/b.mp4"]  # in the order sent


async def test_no_turn_on_automation(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Without an automation nothing can wake Kodi: say so at once instead of waiting."""
    entry = await setup_better_kodi(hass, kodi_source)
    fakes[kodi_source.host].offline = True
    hass.states.async_set(kodi_source.entity_id, "off")
    kodi_integration_turn_on(hass, [])
    with pytest.raises(HomeAssistantError) as err:
        await play(hass, own_entity_id(hass, entry))
    assert err.value.translation_key == "no_turn_on_automation"


async def test_no_wake_when_kodi_is_on(
    hass: HomeAssistant, asleep, kodi_source: KodiSource
) -> None:
    _, player, kodi, events = asleep
    kodi.offline = False
    hass.states.async_set(kodi_source.entity_id, "idle")
    await play(hass, player)
    assert events == []
    assert kodi.files() == [FILE]


async def test_wake_times_out(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """The automation runs, but Kodi never wakes (for example WoL is off in the BIOS)."""
    entry = await setup_better_kodi(hass, kodi_source)
    kodi = fakes[kodi_source.host]
    kodi.offline = True
    hass.states.async_set(kodi_source.entity_id, "off")
    events: list[str] = []
    kodi_integration_turn_on(hass, events)
    hass.bus.async_listen("kodi.turn_on", callback(lambda event: events.append("automation")))
    hass.config_entries.async_update_entry(
        entry, options={CONF_WAKE: True, CONF_WAKE_TIMEOUT: 0.05}
    )
    with pytest.raises(HomeAssistantError) as err:
        await play(hass, own_entity_id(hass, entry))
    assert err.value.translation_key == "wake_timeout"
    assert err.value.translation_placeholders["entity_id"] == kodi_source.entity_id
    assert events == ["turn_on", "automation"]
    assert kodi.files() == []


async def test_wake_can_be_turned_off(hass: HomeAssistant, asleep) -> None:
    entry, player, _, events = asleep
    hass.config_entries.async_update_entry(
        entry, options={CONF_WAKE: False, CONF_WAKE_TIMEOUT: 120}
    )
    with pytest.raises(HomeAssistantError) as err:
        await play(hass, player)
    assert err.value.translation_key == "cannot_connect"
    assert events == []


async def test_button_wakes_kodi_and_keeps_link_on_failure(hass: HomeAssistant, asleep) -> None:
    entry, _, kodi, events = asleep
    registry = er.async_get(hass)
    link = registry.async_get_entity_id(TEXT_DOMAIN, DOMAIN, f"{entry.entry_id}_link")
    button = registry.async_get_entity_id(BUTTON_DOMAIN, DOMAIN, f"{entry.entry_id}_play_next")
    await hass.services.async_call(
        TEXT_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: link, "value": FILE}, blocking=True
    )
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    assert kodi.files() == [FILE]
    assert events == ["turn_on", "automation", "awake"]


async def test_handover_wakes_the_destination(
    hass: HomeAssistant, make_kodi_source, fakes: dict[str, FakeKodi], kodi_source: KodiSource
) -> None:
    living = await setup_better_kodi(hass, kodi_source)
    bedroom_source = await make_kodi_source("Schlafzimmer Kodi", "10.0.0.6")
    bedroom = await setup_better_kodi(hass, bedroom_source)
    living_player, bedroom_player = own_entity_id(hass, living), own_entity_id(hass, bedroom)
    await play(hass, living_player, "replace")

    bedroom_kodi = fakes[bedroom_source.host]
    bedroom_kodi.offline = True
    hass.states.async_set(bedroom_source.entity_id, "off")
    events: list[str] = []
    kodi_integration_turn_on(hass, events)
    wake_on_lan_automation(hass, bedroom_source, bedroom_kodi, events)
    hass.services.async_register(MP_DOMAIN, "media_stop", lambda call: None)
    await hass.services.async_call(
        DOMAIN,
        "handover",
        {ATTR_ENTITY_ID: living_player, "destination": bedroom_player},
        blocking=True,
    )
    assert events == ["turn_on", "automation", "awake"]
    assert bedroom_kodi.files() == [FILE]


async def test_reload_during_wake_does_not_wake_twice(hass: HomeAssistant, asleep) -> None:
    """Changing options reloads the entry; the running wake must be shared, not restarted."""
    entry, player, kodi, events = asleep
    first = hass.async_create_task(play(hass, player))
    await asyncio.sleep(0)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await play(hass, player)
    await first
    assert events.count("turn_on") == 1
    assert kodi.files() == [FILE, FILE]


async def test_bad_request_does_not_wake(hass: HomeAssistant, asleep) -> None:
    _, player, _, events = asleep
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            MP_DOMAIN,
            "play_media",
            {
                ATTR_ENTITY_ID: player,
                ATTR_MEDIA_CONTENT_ID: "3",
                ATTR_MEDIA_CONTENT_TYPE: "channel",
                "enqueue": "add",
            },
            blocking=True,
        )
    assert err.value.translation_key == "enqueue_not_supported"
    assert events == []


async def handover_setup(hass: HomeAssistant, make_kodi_source, fakes, kodi_source):
    living = await setup_better_kodi(hass, kodi_source)
    bedroom_source = await make_kodi_source("Schlafzimmer Kodi", "10.0.0.6")
    bedroom = await setup_better_kodi(hass, bedroom_source)
    bedroom_kodi = fakes[bedroom_source.host]
    bedroom_kodi.offline = True
    hass.states.async_set(bedroom_source.entity_id, "off")
    events: list[str] = []
    kodi_integration_turn_on(hass, events)
    wake_on_lan_automation(hass, bedroom_source, bedroom_kodi, events)
    hass.services.async_register(MP_DOMAIN, "media_stop", lambda call: None)
    return own_entity_id(hass, living), own_entity_id(hass, bedroom), bedroom_kodi, events


async def test_handover_snapshot_is_taken_after_waking(
    hass: HomeAssistant, make_kodi_source, fakes: dict[str, FakeKodi], kodi_source: KodiSource
) -> None:
    living_player, bedroom_player, bedroom_kodi, events = await handover_setup(
        hass, make_kodi_source, fakes, kodi_source
    )
    living_kodi = fakes[kodi_source.host]
    assert events == []
    await play(hass, living_player, "replace")
    living_kodi.time = {"hours": 0, "minutes": 1, "seconds": 0, "milliseconds": 0}

    @callback
    def later_in_the_video(event) -> None:
        # While the bedroom wakes, the living room keeps playing.
        living_kodi.time = {"hours": 0, "minutes": 3, "seconds": 0, "milliseconds": 0}

    hass.bus.async_listen("kodi.turn_on", later_in_the_video)
    await hass.services.async_call(
        DOMAIN,
        "handover",
        {ATTR_ENTITY_ID: living_player, "destination": bedroom_player},
        blocking=True,
    )
    assert bedroom_kodi.seeks == [
        {"time": {"hours": 0, "minutes": 3, "seconds": 0, "milliseconds": 0}}
    ]


async def test_handover_with_nothing_playing_does_not_wake(
    hass: HomeAssistant, make_kodi_source, fakes: dict[str, FakeKodi], kodi_source: KodiSource
) -> None:
    living_player, bedroom_player, _, events = await handover_setup(
        hass, make_kodi_source, fakes, kodi_source
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "handover",
            {ATTR_ENTITY_ID: living_player, "destination": bedroom_player},
            blocking=True,
        )
    assert err.value.translation_key == "nothing_playing"
    assert events == []


async def test_button_keeps_link_while_player_unavailable(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    registry = er.async_get(hass)
    link = registry.async_get_entity_id(TEXT_DOMAIN, DOMAIN, f"{entry.entry_id}_link")
    button = registry.async_get_entity_id(BUTTON_DOMAIN, DOMAIN, f"{entry.entry_id}_play_now")
    await hass.services.async_call(
        TEXT_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: link, "value": FILE}, blocking=True
    )
    hass.states.async_set(kodi_source.entity_id, "unavailable")
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
        )
    assert err.value.translation_key == "no_player"
    assert hass.states.get(link).state == FILE
