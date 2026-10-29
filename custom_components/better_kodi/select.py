"""Select entity: how many played entries stay in the queue."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BetterKodiConfigEntry
from .const import KEEP_PLAYED_KEY, KEEP_PLAYED_OFF, KEEP_PLAYED_OPTIONS
from .entity import BetterKodiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the select entity for played entries."""
    async_add_entities([BetterKodiKeepPlayed(hass, entry)])


class BetterKodiKeepPlayed(BetterKodiEntity, SelectEntity, RestoreEntity):
    """Played entries kept in front of the playing one: off, or how many."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(hass, entry, KEEP_PLAYED_KEY)
        self._attr_options = list(KEEP_PLAYED_OPTIONS)

    async def async_added_to_hass(self) -> None:
        """Restore the choice from before a restart or reload."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in KEEP_PLAYED_OPTIONS:
            self._data.keep_played = _keep(last.state)
        self._data.schedule_trim()

    @property
    def current_option(self) -> str:
        """Return the current choice."""
        keep = self._data.keep_played
        return KEEP_PLAYED_OFF if keep is None else str(keep)

    async def async_select_option(self, option: str) -> None:
        """Store a new choice and tidy up the queue right away."""
        self._data.keep_played = _keep(option)
        self.async_write_ha_state()
        self._data.schedule_trim()


def _keep(option: str) -> int | None:
    """Return how many played entries stay, or None for off."""
    return None if option == KEEP_PLAYED_OFF else int(option)
