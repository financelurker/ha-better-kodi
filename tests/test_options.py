"""Tests for the options: power, media browser choices, link entities, online lookups."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.better_kodi import backends
from custom_components.better_kodi.backends import (
    BackendContext,
    BackendItem,
    MediaSourceBackend,
)
from custom_components.better_kodi.const import (
    CONF_BROADCAST,
    CONF_BROWSE_CHOICES,
    CONF_LINK_ENTITIES,
    CONF_MAC,
    CONF_ONLINE_LOOKUPS,
    CONF_POWER_OFF,
    CONF_POWER_ON,
    CONF_WAKE,
    CONF_WAKE_TIMEOUT,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.better_kodi.media_player import BetterKodiMediaPlayer
from custom_components.better_kodi.queue import KodiQueue
from custom_components.better_kodi.resolvers import ADDON_YOUTUBE, YOUTUBE_OEMBED
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DATA_COMPONENT,
    DOMAIN as MP_DOMAIN,
)
from homeassistant.components.media_source import PlayMedia
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def no_mac_read_delay():
    with patch("custom_components.better_kodi.power.MAC_READ_DELAY", 0):
        yield


def power(**changes: Any) -> dict[str, Any]:
    return {
        CONF_POWER_ON: "wake_on_lan",
        CONF_MAC: "aa-bb-cc-dd-ee-ff",
        CONF_BROADCAST: "192.168.1.255",
        CONF_POWER_OFF: "suspend",
        CONF_WAKE: True,
        CONF_WAKE_TIMEOUT: 90.0,
        **changes,
    }


FEATURES = {CONF_BROWSE_CHOICES: False, CONF_LINK_ENTITIES: True, CONF_ONLINE_LOOKUPS: False}


def suggested(result: dict[str, Any], section: str, key: str) -> Any:
    schema = result["data_schema"].schema
    (section_key,) = [k for k in schema if k == section]
    for field in schema[section_key].schema.schema:
        if field == key:
            return (field.description or {}).get("suggested_value")
    raise AssertionError(key)


async def test_options_flow_reads_the_mac_from_kodi(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    # Kodi first answers "Busy", then the address.
    assert suggested(result, "power", CONF_MAC) == "AA:BB:CC:DD:EE:FF"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power": power(), "features": FEATURES}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        **DEFAULT_OPTIONS,
        CONF_POWER_ON: "wake_on_lan",
        CONF_MAC: "AA:BB:CC:DD:EE:FF",
        CONF_BROADCAST: "192.168.1.255",
        CONF_POWER_OFF: "suspend",
        CONF_WAKE_TIMEOUT: 90,
        **FEATURES,
    }


async def test_options_flow_with_kodi_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    fakes[kodi_source.host].offline = True
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert suggested(result, "power", CONF_MAC) is None

    # Wake on LAN without a MAC address is refused.
    without_mac = power()
    del without_mac[CONF_MAC]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power": without_mac, "features": FEATURES}
    )
    assert result["errors"] == {"base": "mac_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power": power(**{CONF_MAC: "nope"}), "features": FEATURES}
    )
    assert result["errors"] == {"base": "invalid_mac"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power": power(**{CONF_BROADCAST: "kodi.lan"}), "features": FEATURES}
    )
    assert result["errors"] == {"base": "invalid_broadcast"}


async def test_options_flow_keeps_defaults_and_allows_clearing_the_mac(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_MAC: "AA:BB:CC:DD:EE:FF"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    automation = power(**{CONF_POWER_ON: "automation", CONF_POWER_OFF: "automation"})
    del automation[CONF_MAC]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "power": automation,
            "features": {key: True for key in FEATURES},
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MAC] == ""
    assert entry.options[CONF_POWER_ON] == "automation"


def link_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    return sorted(
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registry_entry.domain in ("text", "button")
    )


async def test_link_entities_can_be_switched_off(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    assert len(link_entities(hass, entry)) == 5

    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_LINK_ENTITIES: False}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert link_entities(hass, entry) == []
    assert hass.states.get(own_entity_id(hass, entry)) is not None

    hass.config_entries.async_update_entry(entry, options=DEFAULT_OPTIONS)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(link_entities(hass, entry)) == 5
    assert await hass.config_entries.async_unload(entry.entry_id)


MOVIE = {
    "title": "Film",
    "media_class": "movie",
    "media_content_id": "17",
    "media_content_type": "movie",
    "can_play": True,
    "can_expand": False,
}


async def test_media_browser_choices_can_be_switched_off(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_BROWSE_CHOICES: False}
    )
    async_mock_service(
        hass,
        MP_DOMAIN,
        "browse_media",
        response={
            kodi_source.entity_id: {
                "title": "Movies",
                "media_class": "directory",
                "media_content_id": "",
                "media_content_type": "library_movies",
                "can_play": False,
                "can_expand": True,
                "children": [MOVIE],
            }
        },
        supports_response=SupportsResponse.ONLY,
    )
    entity = hass.data[DATA_COMPONENT].get_entity(own_entity_id(hass, entry))
    assert isinstance(entity, BetterKodiMediaPlayer)
    (movie,) = (await entity.async_browse_media("library_movies", "")).children
    assert (movie.media_content_id, movie.can_expand) == ("17", False)


async def test_online_lookups_can_be_switched_off(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    kodi = fakes[kodi_source.host]
    kodi.addons = {ADDON_YOUTUBE}
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(
        entry, options={**DEFAULT_OPTIONS, CONF_ONLINE_LOOKUPS: False}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.get(YOUTUBE_OEMBED, json={"title": "Online", "thumbnail_url": "t"})
    player = own_entity_id(hass, entry)
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: f"https://youtu.be/{VIDEO_ID}",
            ATTR_MEDIA_CONTENT_TYPE: "url",
            "enqueue": "add",
        },
        blocking=True,
    )
    # Queued by another app, before Home Assistant knew it
    kodi.playlist.append(
        {"file": "plugin://plugin.video.youtube/play/?video_id=abcdefghijk", "label": "play"}
    )
    queue = await hass.services.async_call(
        DOMAIN, "get_queue", {ATTR_ENTITY_ID: player}, blocking=True, return_response=True
    )
    assert aioclient_mock.call_count == 0
    assert all(item["thumbnail"] is None for item in queue[player]["items"])


async def test_queue_youtube_thumbnail_fallback_off(kodi: FakeKodi) -> None:
    queue = KodiQueue(kodi, youtube_thumbnails=False)  # type: ignore[arg-type]
    kodi.playlist = [{"file": f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}"}]
    assert (await queue.items())[0].thumbnail is None


@pytest.fixture
def kodi() -> FakeKodi:
    return FakeKodi()


class ExampleBackend(MediaSourceBackend):
    """A media server whose stream URLs Kodi has in its library."""

    name = "example"

    def recognizes(self, url: str) -> bool:
        return url.startswith("http://media.example/stream/")

    async def async_kodi_item(self, context: BackendContext, url: str) -> BackendItem | None:
        movie_id = int(url.rsplit("/", 1)[1])
        assert await context.get_addons() == frozenset()
        if movie_id == 0:
            raise RuntimeError("a broken backend must not break playing")
        return BackendItem(item={"movieid": movie_id})


async def test_media_source_backends_are_an_extension_point(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    kodi = fakes[kodi_source.host]
    player = own_entity_id(hass, entry)

    async def play(url: str) -> None:
        with patch(
            "custom_components.better_kodi.media_player.media_source.async_resolve_media",
            return_value=PlayMedia(url, "video/mp4"),
        ):
            await hass.services.async_call(
                MP_DOMAIN,
                "play_media",
                {
                    ATTR_ENTITY_ID: player,
                    ATTR_MEDIA_CONTENT_ID: "media-source://example/x",
                    ATTR_MEDIA_CONTENT_TYPE: "video/mp4",
                    "enqueue": "add",
                },
                blocking=True,
            )

    with patch.object(backends, "BACKENDS", (ExampleBackend(), *backends.BACKENDS)):
        await play("http://media.example/stream/42")
        await play("http://media.example/stream/0")
    assert kodi.playlist[0]["id"] == 42
    assert kodi.playlist[1]["file"] == "http://media.example/stream/0"


async def test_mac_is_not_asked_while_kodi_is_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.states.async_set(kodi_source.entity_id, "off")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert suggested(result, "power", CONF_MAC) is None
    assert "XBMC.GetInfoLabels" not in fakes[kodi_source.host].method_calls()


async def test_old_options_are_upgraded_and_broadcast_is_trimmed(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    hass.config_entries.async_update_entry(entry, options={CONF_WAKE: False, CONF_WAKE_TIMEOUT: 60})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"power": power(**{CONF_BROADCAST: " 192.168.1.255 "}), "features": FEATURES},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BROADCAST] == "192.168.1.255"
    assert set(entry.options) == set(DEFAULT_OPTIONS)


async def test_link_entities_switched_off_in_the_options_flow(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "power": power(**{CONF_POWER_ON: "automation"}),
            "features": {**FEATURES, CONF_LINK_ENTITIES: False},
        },
    )
    await hass.async_block_till_done()
    assert link_entities(hass, entry) == []
