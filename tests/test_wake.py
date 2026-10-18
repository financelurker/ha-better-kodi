"""Tests for waking Kodi before sending something to it."""

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
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

FILE = "https://cdn.example/a.mp4"


@pytest.fixture
async def asleep(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> tuple[ConfigEntry, str, FakeKodi, list[str]]:
    """Kodi is suspended; turning it on (the user's WoL automation) wakes it later."""
    kodi = fakes[kodi_source.host]
    entry = await setup_better_kodi(hass, kodi_source)
    kodi.offline = True
    hass.states.async_set(kodi_source.entity_id, "off", {"supported_features": 384})
    events: list[str] = []

    async def turn_on(call: ServiceCall) -> None:
        events.append("turn_on")
        assert call.data[ATTR_ENTITY_ID] == kodi_source.entity_id

        def wake() -> None:
            events.append("awake")
            kodi.offline = False
            hass.states.async_set(kodi_source.entity_id, "idle", {"supported_features": 384})

        hass.loop.call_later(0.05, wake)

    hass.services.async_register(MP_DOMAIN, "turn_on", turn_on)
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
    assert events == ["turn_on", "awake"]
    assert kodi.files() == [FILE]


async def test_no_wake_when_kodi_is_on(
    hass: HomeAssistant, asleep, kodi_source: KodiSource
) -> None:
    _, player, kodi, events = asleep
    kodi.offline = False
    hass.states.async_set(kodi_source.entity_id, "idle")
    await play(hass, player)
    assert events == []
    assert kodi.files() == [FILE]


async def test_wake_times_out(hass: HomeAssistant, asleep, kodi_source: KodiSource) -> None:
    entry, player, kodi, events = asleep

    async def broken_turn_on(call: ServiceCall) -> None:
        events.append("turn_on")  # no WoL automation: nothing happens

    hass.services.async_register(MP_DOMAIN, "turn_on", broken_turn_on)
    hass.config_entries.async_update_entry(
        entry, options={CONF_WAKE: True, CONF_WAKE_TIMEOUT: 0.05}
    )
    with pytest.raises(HomeAssistantError) as err:
        await play(hass, player)
    assert err.value.translation_key == "wake_timeout"
    assert err.value.translation_placeholders["entity_id"] == kodi_source.entity_id
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
    assert events == ["turn_on", "awake"]


async def test_options_flow(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WAKE: True, CONF_WAKE_TIMEOUT: 90.0}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_WAKE: True, CONF_WAKE_TIMEOUT: 90}


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
    woken: list[str] = []

    async def turn_on(call: ServiceCall) -> None:
        woken.append(call.data[ATTR_ENTITY_ID])
        bedroom_kodi.offline = False
        hass.states.async_set(bedroom_source.entity_id, "idle")

    hass.services.async_register(MP_DOMAIN, "turn_on", turn_on)
    hass.services.async_register(MP_DOMAIN, "media_stop", lambda call: None)
    await hass.services.async_call(
        DOMAIN,
        "handover",
        {ATTR_ENTITY_ID: living_player, "destination": bedroom_player},
        blocking=True,
    )
    assert woken == [bedroom_source.entity_id]
    assert bedroom_kodi.files() == [FILE]
