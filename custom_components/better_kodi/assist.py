"""Move Assist settings between the Kodi entity and Better Kodi's entity."""

from homeassistant.components.homeassistant import exposed_entities
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er


@callback
def exposed_assistants(hass: HomeAssistant, entity_id: str) -> dict[str, bool]:
    """Return the assistants that have an explicit expose setting for an entity."""
    result: dict[str, bool] = {}
    for assistant, options in exposed_entities.async_get_entity_settings(hass, entity_id).items():
        if "should_expose" in options:
            result[assistant] = bool(options["should_expose"])
    return result


@callback
def transfer_exposure(
    hass: HomeAssistant, from_entity_id: str, to_entity_id: str, *, unexpose_source: bool
) -> None:
    """Give one entity the expose settings of another.

    With unexpose_source, the other entity is no longer exposed afterwards, so
    Assist does not see two media players with the same name.
    """
    for assistant, exposed in exposed_assistants(hass, from_entity_id).items():
        exposed_entities.async_expose_entity(hass, assistant, to_entity_id, exposed)
        if unexpose_source and exposed:
            exposed_entities.async_expose_entity(hass, assistant, from_entity_id, False)


@callback
def copy_aliases(hass: HomeAssistant, from_entity_id: str, to_entity_id: str) -> None:
    """Add the aliases of one entity to another, keeping the existing ones."""
    registry = er.async_get(hass)
    source = registry.async_get(from_entity_id)
    target = registry.async_get(to_entity_id)
    if source is None or target is None:
        return
    missing = [alias for alias in source.aliases if alias not in target.aliases]
    if missing:
        registry.async_update_entity(to_entity_id, aliases=[*target.aliases, *missing])
