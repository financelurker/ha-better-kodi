"""Link field: paste a link, then press a button to play or queue it."""

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BetterKodiConfigEntry
from .entity import BetterKodiEntity

MAX_LINK_LENGTH = 255  # longest state Home Assistant can store


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the link field."""
    async_add_entities([BetterKodiLink(hass, entry)])


class BetterKodiLink(BetterKodiEntity, TextEntity):
    """The link that the buttons play or queue."""

    _attr_mode = TextMode.TEXT
    _attr_native_max = MAX_LINK_LENGTH
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry) -> None:
        """Initialize the link field."""
        super().__init__(hass, entry, "link")

    async def async_added_to_hass(self) -> None:
        """Follow changes made by the buttons (they clear the field)."""
        self._data.link_listeners.append(self._on_link_changed)
        self.async_on_remove(lambda: self._data.link_listeners.remove(self._on_link_changed))

    @property
    def native_value(self) -> str:
        """Return the link."""
        return self._data.link

    async def async_set_value(self, value: str) -> None:
        """Store a new link."""
        self._data.set_link(value.strip())

    @callback
    def _on_link_changed(self) -> None:
        self.async_write_ha_state()
