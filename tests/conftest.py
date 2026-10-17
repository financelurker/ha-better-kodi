"""Shared fixtures for Better Kodi tests."""

from collections.abc import Callable, Coroutine, Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_kodi.const import (
    CONF_HIDE_SOURCE,
    CONF_SOURCE_ENTITY_ID,
    CONF_SOURCE_ENTITY_UUID,
    CONF_WS_PORT,
    DOMAIN,
)
from custom_components.better_kodi.kodi_client import KodiClient
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .fake_kodi import FakeKodi

SOURCE_FEATURES = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return


@dataclass
class KodiSource:
    """A Kodi media player of the built-in integration, as Better Kodi sees it."""

    entity_id: str
    uuid: str
    device_id: str
    config_entry: MockConfigEntry
    host: str


@pytest.fixture
def fakes() -> dict[str, FakeKodi]:
    """Simulated Kodi instances by host."""
    return {}


@pytest.fixture(autouse=True)
def patch_kodi_client(fakes: dict[str, FakeKodi]) -> Generator[None]:
    """Route Better Kodi's JSON-RPC calls to the fake Kodi of the host."""

    async def call(self: KodiClient, method: str, params: dict[str, Any] | None = None) -> Any:
        return await fakes[self._host].call(method, params)

    async def no_notifications(self: KodiClient, callback: Any) -> None:
        return None

    with (
        patch.object(KodiClient, "call", call),
        patch.object(KodiClient, "run_notifications", no_notifications),
    ):
        yield


@pytest.fixture
async def base_components(hass: HomeAssistant) -> None:
    """Set up what a real Home Assistant always has."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "media_player", {})
    # The built-in Kodi integration is "set up" without connecting anywhere.
    hass.config.components.add("kodi")


type SourceFactory = Callable[..., Coroutine[Any, Any, KodiSource]]


@pytest.fixture
def make_kodi_source(
    hass: HomeAssistant, base_components: None, fakes: dict[str, FakeKodi]
) -> SourceFactory:
    """Create a Kodi entity of the built-in integration plus a fake Kodi."""

    async def factory(name: str = "Wohnzimmer Kodi", host: str = "10.0.0.5") -> KodiSource:
        fakes[host] = FakeKodi()
        unique = f"kodi-{host}"
        kodi_entry = MockConfigEntry(
            domain="kodi",
            unique_id=unique,
            title=name,
            data={
                CONF_HOST: host,
                CONF_PORT: 8080,
                CONF_WS_PORT: 9090,
                CONF_USERNAME: "kodi",
                CONF_PASSWORD: "secret",
                CONF_SSL: False,
                "name": name,
                "timeout": 5,
            },
        )
        kodi_entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=kodi_entry.entry_id,
            identifiers={("kodi", unique)},
            manufacturer="Kodi",
            name=name,
        )
        registry_entry = er.async_get(hass).async_get_or_create(
            "media_player",
            "kodi",
            unique,
            config_entry=kodi_entry,
            device_id=device.id,
            has_entity_name=True,
            original_name=None,
        )
        hass.states.async_set(
            registry_entry.entity_id,
            "playing",
            {
                "friendly_name": name,
                "supported_features": int(SOURCE_FEATURES),
                "volume_level": 0.5,
                "is_volume_muted": False,
                "media_title": "Something",
                "media_content_type": "video",
                "entity_picture": f"/api/media_player_proxy/{registry_entry.entity_id}?token=abc",
                "dynamic_range": "hdr10",
            },
        )
        return KodiSource(
            entity_id=registry_entry.entity_id,
            uuid=registry_entry.id,
            device_id=device.id,
            config_entry=kodi_entry,
            host=host,
        )

    return factory


@pytest.fixture
async def kodi_source(make_kodi_source: SourceFactory) -> KodiSource:
    """One Kodi entity of the built-in integration."""
    return await make_kodi_source()


def better_kodi_entry(source: KodiSource, *, hide: bool = True) -> MockConfigEntry:
    """Create a Better Kodi config entry for a Kodi source."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=source.uuid,
        title="Wohnzimmer Kodi",
        data={
            CONF_SOURCE_ENTITY_ID: source.entity_id,
            CONF_SOURCE_ENTITY_UUID: source.uuid,
            CONF_HIDE_SOURCE: hide,
            CONF_HOST: source.host,
            CONF_PORT: 8080,
            CONF_WS_PORT: 9090,
            CONF_USERNAME: "kodi",
            CONF_PASSWORD: "secret",
            CONF_SSL: False,
        },
    )


async def setup_better_kodi(hass: HomeAssistant, source: KodiSource) -> MockConfigEntry:
    """Set up Better Kodi for a source and return its entry."""
    entry = better_kodi_entry(source)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def own_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity ID of Better Kodi's media player."""
    entity_id = er.async_get(hass).async_get_entity_id("media_player", DOMAIN, entry.entry_id)
    assert entity_id is not None
    return entity_id
