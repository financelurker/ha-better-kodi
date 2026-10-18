"""Buttons that play or queue the link from the link field."""

from homeassistant.components.button import ButtonEntity
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ENQUEUE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    MediaPlayerEnqueue,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BetterKodiConfigEntry
from .const import DOMAIN
from .entity import BetterKodiEntity

BUTTONS = {
    "play_now": MediaPlayerEnqueue.PLAY,
    "play_next": MediaPlayerEnqueue.NEXT,
    "add_to_queue": MediaPlayerEnqueue.ADD,
    "replace_queue": MediaPlayerEnqueue.REPLACE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the four buttons."""
    async_add_entities(BetterKodiLinkButton(hass, entry, key) for key in BUTTONS)


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
        if (player := self.player_entity_id()) is None:
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
