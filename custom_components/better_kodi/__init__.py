"""Better Kodi: queue, URL casting and handover on top of the Kodi integration."""

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

import aiohttp

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
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import backends
from .assist import transfer_exposure
from .const import (
    CONF_HIDE_SOURCE,
    CONF_LINK_ENTITIES,
    CONF_MAINTENANCE_BUTTONS,
    CONF_NOTIFY_ENTITY,
    CONF_ONLINE_LOOKUPS,
    CONF_SOURCE_ENTITY_UUID,
    CONF_WS_PORT,
    DEFAULT_OPTIONS,
    DOMAIN,
    LINK_KEYS,
    MAINTENANCE_KEYS,
    NOTIFY_KEY,
    OPTIONAL_KEYS,
)
from .kodi_client import KodiAuthError, KodiClient, KodiError
from .queue import KodiQueue
from .resolvers import async_youtube_title
from .services import async_setup_services
from .share import async_setup_share

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
ADDON_CACHE_SECONDS = 600
NOTIFICATIONS = ("Playlist.", "Player.OnPlay", "Player.OnStop", "Player.OnAVStart")

type BetterKodiConfigEntry = ConfigEntry[BetterKodiData]
type QueueListener = Callable[[], None]


@dataclass
class BetterKodiData:
    """Runtime data of one Better Kodi entry."""

    client: KodiClient
    queue: KodiQueue
    source_entity_id: str
    web_session: aiohttp.ClientSession
    addons: frozenset[str] = frozenset()
    addons_loaded_at: float | None = None
    queue_listeners: list[QueueListener] = field(default_factory=list)
    link: str = ""
    link_listeners: list[QueueListener] = field(default_factory=list)
    platforms: list[Platform] = field(default_factory=lambda: list(PLATFORMS))

    @property
    def backend_context(self) -> backends.BackendContext:
        """Return what media-source backends may use."""
        return backends.BackendContext(
            session=self.web_session, client=self.client, get_addons=self.async_get_addons
        )

    def set_link(self, link: str) -> None:
        """Store the link of the link field and tell its entity."""
        self.link = link
        for listener in list(self.link_listeners):
            listener()

    async def async_get_addons(self, *, force: bool = False) -> frozenset[str]:
        """Return the IDs of enabled video add-ons, refreshed every few minutes."""
        stale = (
            self.addons_loaded_at is None
            or time.monotonic() - self.addons_loaded_at > ADDON_CACHE_SECONDS
        )
        if force or stale:
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
    """Create a Kodi client from config entry data.

    Kodi often uses a self-signed certificate, so certificates are not checked
    for Kodi itself. Requests to websites use a separate, verifying session.
    """
    return KodiClient(
        async_get_clientsession(hass, verify_ssl=False),
        data[CONF_HOST],
        data[CONF_PORT],
        username=data.get(CONF_USERNAME) or None,
        password=data.get(CONF_PASSWORD) or None,
        ssl=data.get(CONF_SSL, False),
        ws_port=data.get(CONF_WS_PORT) or None,
    )


def get_option(entry: ConfigEntry, key: str) -> Any:
    """Return an option of an entry, or its default."""
    return entry.options.get(key, DEFAULT_OPTIONS[key])


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Better Kodi services and listen for links shared from phones."""
    async_setup_services(hass)
    async_setup_share(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BetterKodiConfigEntry) -> bool:
    """Set up Better Kodi; Kodi itself may be switched off at this point."""
    source_entity_id = er.async_resolve_entity_id(
        er.async_get(hass), entry.data[CONF_SOURCE_ENTITY_UUID]
    )
    if source_entity_id is None:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="source_missing")

    client = client_from_entry_data(hass, dict(entry.data))
    try:
        await client.ping()
    except KodiAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except KodiError as err:
        # Like the Kodi integration: Kodi (or the TV) is off. Better Kodi still
        # starts, so that turn_on works; the rest follows once Kodi answers.
        _LOGGER.debug("Kodi at %s is not reachable yet: %s", entry.data[CONF_HOST], err)

    web_session = async_get_clientsession(hass)
    online_lookups = bool(get_option(entry, CONF_ONLINE_LOOKUPS))

    async def look_up_title(file: str) -> tuple[str | None, str | None] | None:
        if (found := await backends.async_title(data.backend_context, file)) is not None:
            return found
        return await async_youtube_title(web_session, file) if online_lookups else None

    data = BetterKodiData(
        client=client,
        queue=KodiQueue(client, look_up_title, youtube_thumbnails=online_lookups),
        source_entity_id=source_entity_id,
        web_session=web_session,
    )
    wanted: set[str] = set()
    links, maintenance = (
        get_option(entry, CONF_LINK_ENTITIES),
        get_option(entry, CONF_MAINTENANCE_BUTTONS),
    )
    if links:
        wanted.update(LINK_KEYS)
        data.platforms.append(Platform.TEXT)
    if maintenance:
        wanted.update(MAINTENANCE_KEYS)
    if links or maintenance:
        data.platforms.append(Platform.BUTTON)
    if get_option(entry, CONF_NOTIFY_ENTITY):
        wanted.add(NOTIFY_KEY)
        data.platforms.append(Platform.NOTIFY)
    _remove_switched_off_entities(hass, entry, wanted)
    await data.async_get_addons(force=True)
    entry.runtime_data = data

    def on_notification(method: str, payload: dict[str, Any]) -> None:
        if method.startswith(NOTIFICATIONS):
            data.notify_queue_changed()

    entry.async_create_background_task(
        hass,
        client.run_notifications(on_notification),
        f"{DOMAIN} notifications {entry.entry_id}",
    )
    await hass.config_entries.async_forward_entry_setups(entry, data.platforms)
    return True


def _remove_switched_off_entities(
    hass: HomeAssistant, entry: ConfigEntry, wanted: set[str]
) -> None:
    """Remove the entities that were switched off in the options (link, buttons, notify)."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = registry_entry.unique_id.removeprefix(prefix)
        if key in OPTIONAL_KEYS and key not in wanted:
            registry.async_remove(registry_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: BetterKodiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, entry.runtime_data.platforms)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give the Kodi entity back what Better Kodi took over."""
    registry = er.async_get(hass)
    source_entity_id = er.async_resolve_entity_id(registry, entry.data[CONF_SOURCE_ENTITY_UUID])
    if source_entity_id is None:
        return
    source = registry.async_get(source_entity_id)
    if (
        entry.data.get(CONF_HIDE_SOURCE)
        and source is not None
        and source.hidden_by is er.RegistryEntryHider.INTEGRATION
    ):
        registry.async_update_entity(source_entity_id, hidden_by=None)

    if own_entity_id := registry.async_get_entity_id("media_player", DOMAIN, entry.entry_id):
        transfer_exposure(hass, own_entity_id, source_entity_id, unexpose_source=False)
