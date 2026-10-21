"""Notify entity: show messages on the TV, on top of what Kodi plays."""

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BetterKodiConfigEntry
from .const import DOMAIN
from .entity import BetterKodiEntity
from .kodi_client import KodiError, KodiRpcError

DISPLAY_TIME_MS = 8000  # long enough to read a sentence across the room


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the notify entity."""
    async_add_entities([BetterKodiNotify(hass, entry)])


class BetterKodiNotify(BetterKodiEntity, NotifyEntity):
    """Messages shown by Kodi (GUI.ShowNotification)."""

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry) -> None:
        """Initialize the notify entity, named after the Kodi device."""
        super().__init__(hass, entry, "notify")
        self._attr_translation_key = None
        self._attr_name = None

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Show the message on Kodi."""
        client = self._data.client
        try:
            await client.call(
                "GUI.ShowNotification",
                {
                    # Kodi needs a title; without one the message is the title line.
                    "title": title or message,
                    "message": message if title else "",
                    "displaytime": DISPLAY_TIME_MS,
                },
            )
        except KodiRpcError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="kodi_error",
                translation_placeholders={"error": err.message},
            ) from err
        except KodiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"host": client.base_url},
            ) from err
