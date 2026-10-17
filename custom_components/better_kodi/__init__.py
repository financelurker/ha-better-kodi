"""Better Kodi: queue, URL casting and handover on top of the Kodi integration."""

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from homeassistant.components.homeassistant import exposed_entities
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import CONF_SOURCE_ENTITY_UUID, CONF_WS_PORT, DOMAIN
from .kodi_client import KodiAuthError, KodiClient, KodiError
from .queue import KodiQueue
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
ADDON_CACHE_SECONDS = 600

type BetterKodiConfigEntry = ConfigEntry[BetterKodiData]
type QueueListener = Callable[[], None]


@dataclass
class BetterKodiData:
    """Runtime data of one Better Kodi entry."""

    client: KodiClient
    queue: KodiQueue
    addons: frozenset[str] = frozenset()
    addons_loaded_at: float = 0.0
    queue_listeners: list[QueueListener] = field(default_factory=list)

    async def async_get_addons(self, *, force: bool = False) -> frozenset[str]:
        """Return the IDs of enabled video add-ons, refreshed every few minutes."""
        if force or time.monotonic() - self.addons_loaded_at > ADDON_CACHE_SECONDS:
            try:
                result = await self.client.call(
                    "Addons.GetAddons",
                    {"type": "xbmc.python.pluginsource", "enabled": True},
                )
            except KodiError as err:
                _LOGGER.debug("Could not read Kodi add-ons: %s", err)
            else:
                self.addons = frozenset(
                    addon["addonid"] for addon in (result or {}).get("addons") or []
                )
                self.addons_loaded_at = time.monotonic()
        return self.addons

    def notify_queue_changed(self) -> None:
        """Tell listeners (the entity) that the queue may have changed."""
        for listener in list(self.queue_listeners):
            listener()


def client_from_entry_data(hass: HomeAssistant, data: dict[str, Any]) -> KodiClient:
    """Create a Kodi client from config entry data."""
    return KodiClient(
        async_get_clientsession(hass, verify_ssl=False),
        data[CONF_HOST],
        data[CONF_PORT],
        username=data.get(CONF_USERNAME) or None,
        password=data.get(CONF_PASSWORD) or None,
        ssl=data.get(CONF_SSL, False),
        ws_port=data.get(CONF_WS_PORT) or None,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Better Kodi services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BetterKodiConfigEntry) -> bool:
    """Connect to Kodi and set up the media player."""
    client = client_from_entry_data(hass, dict(entry.data))
    try:
        await client.ping()
    except KodiAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except KodiError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": entry.data[CONF_HOST]},
        ) from err

    data = BetterKodiData(client=client, queue=KodiQueue(client))
    await data.async_get_addons(force=True)
    entry.runtime_data = data

    def on_notification(method: str, payload: dict[str, Any]) -> None:
        if method.startswith(("Playlist.", "Player.OnPlay", "Player.OnStop", "Player.OnAVStart")):
            data.notify_queue_changed()

    entry.async_create_background_task(
        hass,
        client.run_notifications(on_notification),
        f"{DOMAIN} notifications {entry.entry_id}",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BetterKodiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show the Kodi entity again and give it back its Assist settings."""
    registry = er.async_get(hass)
    source_entity_id = er.async_resolve_entity_id(registry, entry.data[CONF_SOURCE_ENTITY_UUID])
    if source_entity_id is None:
        return
    source = registry.async_get(source_entity_id)
    if source is not None and source.hidden_by is er.RegistryEntryHider.INTEGRATION:
        registry.async_update_entity(source_entity_id, hidden_by=None)

    own_entity_id = registry.async_get_entity_id("media_player", DOMAIN, entry.entry_id)
    if own_entity_id is None:
        return
    for assistant, settings in exposed_entities.async_get_entity_settings(
        hass, own_entity_id
    ).items():
        if (should_expose := settings.get("should_expose")) is not None:
            exposed_entities.async_expose_entity(hass, assistant, source_entity_id, should_expose)
