"""Tests for the link field and its buttons."""

import pytest

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, setup_better_kodi
from .fake_kodi import FakeKodi


def entity_id(hass: HomeAssistant, domain: str, entry_id: str, key: str) -> str:
    found = er.async_get(hass).async_get_entity_id(domain, "better_kodi", f"{entry_id}_{key}")
    assert found is not None
    return found


async def set_link(hass: HomeAssistant, link_entity: str, value: str) -> None:
    await hass.services.async_call(
        TEXT_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: link_entity, "value": value}, blocking=True
    )


async def press(hass: HomeAssistant, button: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )


async def test_link_and_buttons(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    kodi = fakes[kodi_source.host]
    entry = await setup_better_kodi(hass, kodi_source)
    link = entity_id(hass, TEXT_DOMAIN, entry.entry_id, "link")
    registry = er.async_get(hass)
    assert registry.async_get(link).device_id == kodi_source.device_id

    for name, key in (("a", "play_now"), ("b", "add_to_queue"), ("c", "play_next")):
        await set_link(hass, link, f" https://cdn.example/{name}.mp4 ")
        assert hass.states.get(link).state == f"https://cdn.example/{name}.mp4"
        await press(hass, entity_id(hass, BUTTON_DOMAIN, entry.entry_id, key))
        assert hass.states.get(link).state == ""

    assert [f.rsplit("/", 1)[1] for f in kodi.files()] == ["a.mp4", "c.mp4", "b.mp4"]

    await set_link(hass, link, "https://cdn.example/d.mp4")
    await press(hass, entity_id(hass, BUTTON_DOMAIN, entry.entry_id, "replace_queue"))
    assert kodi.files() == ["https://cdn.example/d.mp4"]


async def test_button_without_link(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    with pytest.raises(ServiceValidationError) as err:
        await press(hass, entity_id(hass, BUTTON_DOMAIN, entry.entry_id, "play_now"))
    assert err.value.translation_key == "no_link"


async def test_failed_play_keeps_the_link(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    link = entity_id(hass, TEXT_DOMAIN, entry.entry_id, "link")
    await set_link(hass, link, "https://youtu.be/dQw4w9WgXcQ")  # YouTube add-on missing
    with pytest.raises(HomeAssistantError, match="YouTube add-on"):
        await press(hass, entity_id(hass, BUTTON_DOMAIN, entry.entry_id, "play_now"))
    assert hass.states.get(link).state == "https://youtu.be/dQw4w9WgXcQ"
