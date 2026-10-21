"""Tests for the library and restart buttons and the notify entity."""

import pytest

from custom_components.better_kodi.const import (
    CONF_LINK_ENTITIES,
    CONF_MAINTENANCE_BUTTONS,
    CONF_NOTIFY_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.better_kodi.kodi_client import KodiConnectionError
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.notify import DOMAIN as NOTIFY_DOMAIN, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, setup_better_kodi
from .fake_kodi import FakeKodi


def entity_id(hass: HomeAssistant, entry: ConfigEntry, domain: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")


@pytest.mark.parametrize(
    ("key", "method"),
    [
        ("library_scan", "VideoLibrary.Scan"),
        ("library_clean", "VideoLibrary.Clean"),
        ("reboot", "System.Reboot"),
    ],
)
async def test_buttons(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
    key: str,
    method: str,
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    button = entity_id(hass, entry, BUTTON_DOMAIN, key)
    assert er.async_get(hass).async_get(button).entity_category is EntityCategory.CONFIG
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    assert method in fakes[kodi_source.host].method_calls()


async def test_reboot_not_allowed(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    kodi = fakes[kodi_source.host]
    kodi.power["canreboot"] = False
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id(hass, entry, BUTTON_DOMAIN, "reboot")},
            blocking=True,
        )
    assert err.value.translation_key == "reboot_not_allowed"
    assert "System.Reboot" not in kodi.method_calls()


async def test_button_while_kodi_is_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    fakes[kodi_source.host].offline = True
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id(hass, entry, BUTTON_DOMAIN, "library_scan")},
            blocking=True,
        )
    assert err.value.translation_key == "cannot_connect"


async def test_notify(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    notify = entity_id(hass, entry, NOTIFY_DOMAIN, "notify")
    assert notify == "notify.wohnzimmer_kodi"
    kodi = fakes[kodi_source.host]
    await hass.services.async_call(
        NOTIFY_DOMAIN,
        "send_message",
        {ATTR_ENTITY_ID: notify, "title": "Haustür", "message": "Es hat geklingelt"},
        blocking=True,
    )
    await hass.services.async_call(
        NOTIFY_DOMAIN,
        "send_message",
        {ATTR_ENTITY_ID: notify, "message": "Waschmaschine fertig"},
        blocking=True,
    )
    assert kodi.notifications == [
        {"title": "Haustür", "message": "Es hat geklingelt", "displaytime": 8000},
        {"title": "Wohnzimmer Kodi", "message": "Waschmaschine fertig", "displaytime": 8000},
    ]
    features = hass.states.get(notify).attributes["supported_features"]
    assert features & NotifyEntityFeature.TITLE


async def test_extras_can_be_switched_off(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry,
        options={**DEFAULT_OPTIONS, CONF_MAINTENANCE_BUTTONS: False, CONF_NOTIFY_ENTITY: False},
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_id(hass, entry, NOTIFY_DOMAIN, "notify") is None
    assert entity_id(hass, entry, BUTTON_DOMAIN, "reboot") is None
    assert entity_id(hass, entry, BUTTON_DOMAIN, "play_now") is not None  # link buttons stay
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_clean_shows_no_dialog(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, entry, BUTTON_DOMAIN, "library_clean")},
        blocking=True,
    )
    assert ("VideoLibrary.Clean", {"showdialogs": False}) in fakes[kodi_source.host].calls


async def test_reboot_counts_when_kodi_goes_down_first(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    kodi = fakes[kodi_source.host]

    def drop() -> str:
        raise KodiConnectionError("gone")

    kodi._System_Reboot = drop  # type: ignore[method-assign]
    kodi._VideoLibrary_Scan = drop  # type: ignore[method-assign]
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, entry, BUTTON_DOMAIN, "reboot")},
        blocking=True,
    )
    with pytest.raises(HomeAssistantError) as err:  # not for the library
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id(hass, entry, BUTTON_DOMAIN, "library_scan")},
            blocking=True,
        )
    assert err.value.translation_key == "cannot_connect"


async def test_notify_while_kodi_is_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    fakes[kodi_source.host].offline = True
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            NOTIFY_DOMAIN,
            "send_message",
            {ATTR_ENTITY_ID: entity_id(hass, entry, NOTIFY_DOMAIN, "notify"), "message": "x"},
            blocking=True,
        )
    assert err.value.translation_key == "cannot_connect"


@pytest.mark.parametrize(
    ("links", "maintenance", "notify"),
    [(False, True, True), (True, False, False), (False, False, False)],
)
async def test_option_combinations(
    hass: HomeAssistant, kodi_source: KodiSource, links: bool, maintenance: bool, notify: bool
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **DEFAULT_OPTIONS,
            CONF_LINK_ENTITIES: links,
            CONF_MAINTENANCE_BUTTONS: maintenance,
            CONF_NOTIFY_ENTITY: notify,
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (entity_id(hass, entry, "text", "link") is not None) is links
    assert (entity_id(hass, entry, BUTTON_DOMAIN, "play_now") is not None) is links
    assert (entity_id(hass, entry, BUTTON_DOMAIN, "reboot") is not None) is maintenance
    assert (entity_id(hass, entry, NOTIFY_DOMAIN, "notify") is not None) is notify
    media_player = er.async_get(hass).async_get_entity_id("media_player", DOMAIN, entry.entry_id)
    assert media_player is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
