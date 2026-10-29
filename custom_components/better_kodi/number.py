"""Number entity: how many played entries stay in the queue."""

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BetterKodiConfigEntry
from .const import KEEP_PLAYED_KEY, MAX_KEEP_PLAYED
from .entity import BetterKodiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the number entity for played entries."""
    async_add_entities([BetterKodiKeepPlayed(hass, entry)])


class BetterKodiKeepPlayed(BetterKodiEntity, RestoreNumber):
    """Played entries kept in front of the playing one; 0 keeps all (off)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_KEEP_PLAYED
    _attr_native_step = 1
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(hass, entry, KEEP_PLAYED_KEY)

    async def async_added_to_hass(self) -> None:
        """Restore the value from before a restart or reload."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._data.keep_played = _valid(last.native_value)
        self._data.schedule_trim()

    @property
    def native_value(self) -> int:
        """Return how many played entries stay."""
        return self._data.keep_played

    async def async_set_native_value(self, value: float) -> None:
        """Store a new value and tidy up the queue right away."""
        self._data.keep_played = _valid(value)
        self.async_write_ha_state()
        self._data.schedule_trim()


def _valid(value: float) -> int:
    return min(max(round(value), 0), MAX_KEEP_PLAYED)
