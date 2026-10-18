"""Shared helpers for Better Kodi's link field and buttons."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity import Entity

from . import BetterKodiConfigEntry
from .const import DOMAIN


class BetterKodiEntity(Entity):
    """An entity of Better Kodi on the Kodi device."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry, key: str) -> None:
        """Attach the entity to the Kodi device."""
        self._entry = entry
        self._data = entry.runtime_data
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.device_entry = async_entity_id_to_device(hass, self._data.source_entity_id)

    def player_entity_id(self) -> str | None:
        """Return the entity ID of Better Kodi's media player of this entry."""
        return er.async_get(self.hass).async_get_entity_id(
            "media_player", DOMAIN, self._entry.entry_id
        )
