"""Buttons: play or queue the link from the link field; library and restart."""

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ENQUEUE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    MediaPlayerEnqueue,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BetterKodiConfigEntry, get_option
from .const import CONF_LINK_ENTITIES, CONF_MAINTENANCE_BUTTONS, DOMAIN
from .entity import BetterKodiEntity
from .kodi_client import KodiAuthError, KodiConnectionError, KodiError, KodiRpcError

BUTTONS = {
    "play_now": MediaPlayerEnqueue.PLAY,
    "play_next": MediaPlayerEnqueue.NEXT,
    "add_to_queue": MediaPlayerEnqueue.ADD,
    "replace_queue": MediaPlayerEnqueue.REPLACE,
}

# key -> (Kodi JSON-RPC method, its parameters, capability Kodi must report, device class)
METHOD_BUTTONS: dict[
    str, tuple[str, dict[str, Any] | None, str | None, ButtonDeviceClass | None]
] = {
    "library_scan": ("VideoLibrary.Scan", None, None, None),
    # Cleaning would otherwise put a progress dialog in front of what plays.
    "library_clean": ("VideoLibrary.Clean", {"showdialogs": False}, None, None),
    "reboot": ("System.Reboot", None, "canreboot", ButtonDeviceClass.RESTART),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the link buttons and the maintenance buttons, as chosen in the options."""
    entities: list[ButtonEntity] = []
    if get_option(entry, CONF_LINK_ENTITIES):
        entities.extend(BetterKodiLinkButton(hass, entry, key) for key in BUTTONS)
    if get_option(entry, CONF_MAINTENANCE_BUTTONS):
        entities.extend(BetterKodiMethodButton(hass, entry, key) for key in METHOD_BUTTONS)
    async_add_entities(entities)


class BetterKodiLinkButton(BetterKodiEntity, ButtonEntity):
    """Play or queue the link from the link field."""

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry, key: str) -> None:
        """Initialize a button for one way of queueing."""
        super().__init__(hass, entry, key)
        self._enqueue = BUTTONS[key]

    async def async_press(self) -> None:
        """Send the link to Kodi, then clear the field."""
        if not (link := self._data.link):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_link")
        player = self.player_entity_id()
        state = self.hass.states.get(player) if player else None
        if player is None or state is None or state.state == STATE_UNAVAILABLE:
            # Home Assistant silently skips unavailable entities; don't clear the link then.
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_player")
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: player,
                ATTR_MEDIA_CONTENT_TYPE: "url",
                ATTR_MEDIA_CONTENT_ID: link,
                ATTR_MEDIA_ENQUEUE: self._enqueue,
            },
            blocking=True,
            context=self._context,
        )
        if self._data.link == link:
            self._data.set_link("")


class BetterKodiMethodButton(BetterKodiEntity, ButtonEntity):
    """Update or clean Kodi's video library, or restart the Kodi device."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry, key: str) -> None:
        """Initialize a button for one Kodi command."""
        super().__init__(hass, entry, key)
        self._method, self._params, self._capability, self._attr_device_class = METHOD_BUTTONS[key]

    async def async_press(self) -> None:
        """Send the command to Kodi."""
        self._data.mark_used()
        client = self._data.client
        try:
            if self._capability:
                properties = await client.call(
                    "System.GetProperties", {"properties": [self._capability]}
                )
                if not (properties or {}).get(self._capability):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="reboot_not_allowed"
                    )
            try:
                await client.call(self._method, self._params)
            except KodiConnectionError:
                if self._capability is None:
                    raise
                # Restarting: Kodi may go down before its answer arrives.
        except KodiRpcError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="kodi_error",
                translation_placeholders={"error": err.message},
            ) from err
        except KodiAuthError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except KodiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"host": client.base_url},
            ) from err
