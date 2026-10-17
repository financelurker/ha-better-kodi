"""Config flow for Better Kodi."""

from typing import Any

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector

from . import client_from_entry_data
from .const import (
    CONF_HIDE_SOURCE,
    CONF_SOURCE_ENTITY_ID,
    CONF_SOURCE_ENTITY_UUID,
    CONF_WS_PORT,
    DEFAULT_PORT,
    DEFAULT_WS_PORT,
    DOMAIN,
    KODI_DOMAIN,
)
from .kodi_client import KodiAuthError, KodiError

CONNECTION_KEYS = (CONF_HOST, CONF_PORT, CONF_WS_PORT, CONF_USERNAME, CONF_PASSWORD, CONF_SSL)


def connection_defaults_from_kodi(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Read connection data from the Kodi integration's config entry.

    This relies on the Kodi integration's internal data keys. If they change,
    the form is simply not pre-filled.
    """
    entry_id = er.async_get(hass).async_get(entity_id).config_entry_id  # type: ignore[union-attr]
    kodi_entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if kodi_entry is None:
        return {}
    return {key: kodi_entry.data[key] for key in CONNECTION_KEYS if key in kodi_entry.data}


def connection_schema(defaults: dict[str, Any]) -> probatio.Schema:
    """Return the form for the Kodi connection."""

    def default(key: str, fallback: Any = None) -> dict[str, Any]:
        value = defaults.get(key, fallback)
        return {} if value is None else {"default": value}

    def suggested(key: str, fallback: Any = None) -> dict[str, Any]:
        # Optional fields use a suggested value, so they can be cleared.
        value = defaults.get(key, fallback)
        return {} if value is None else {"description": {"suggested_value": value}}

    return probatio.Schema(
        {
            probatio.Required(CONF_HOST, **default(CONF_HOST)): selector.TextSelector(),
            probatio.Required(
                CONF_PORT, **default(CONF_PORT, DEFAULT_PORT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                )
            ),
            probatio.Optional(
                CONF_WS_PORT, **suggested(CONF_WS_PORT, DEFAULT_WS_PORT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                )
            ),
            probatio.Optional(CONF_USERNAME, **suggested(CONF_USERNAME)): selector.TextSelector(),
            probatio.Optional(CONF_PASSWORD, **suggested(CONF_PASSWORD)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            probatio.Required(CONF_SSL, **default(CONF_SSL, False)): selector.BooleanSelector(),
        }
    )


def normalize_connection(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn form values into stored connection data."""
    data = {key: user_input.get(key) for key in CONNECTION_KEYS}
    data[CONF_PORT] = int(data[CONF_PORT])
    data[CONF_WS_PORT] = int(data[CONF_WS_PORT]) if data.get(CONF_WS_PORT) else None
    data[CONF_SSL] = bool(data.get(CONF_SSL))
    return data


async def validate_connection(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """Return an error key, or None if Kodi answers."""
    try:
        await client_from_entry_data(hass, data).ping()
    except KodiAuthError:
        return "invalid_auth"
    except KodiError:
        return "cannot_connect"
    return None


class BetterKodiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Better Kodi for one Kodi entity."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._source: dict[str, Any] = {}
        self._defaults: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the Kodi entity to extend."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_SOURCE_ENTITY_ID]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is None or registry_entry.platform != KODI_DOMAIN:
                errors[CONF_SOURCE_ENTITY_ID] = "not_kodi"
            else:
                await self.async_set_unique_id(registry_entry.id)
                self._abort_if_unique_id_configured()
                self._source = {
                    CONF_SOURCE_ENTITY_ID: entity_id,
                    CONF_SOURCE_ENTITY_UUID: registry_entry.id,
                    CONF_HIDE_SOURCE: user_input[CONF_HIDE_SOURCE],
                }
                self._defaults = connection_defaults_from_kodi(self.hass, entity_id)
                return await self.async_step_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_SOURCE_ENTITY_ID): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="media_player",
                            integration=KODI_DOMAIN,
                            exclude_entities=self._configured_sources(),
                        )
                    ),
                    probatio.Required(CONF_HIDE_SOURCE, default=True): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm or adjust the connection to Kodi."""
        errors: dict[str, str] = {}
        if user_input is not None:
            connection = normalize_connection(user_input)
            if (error := await validate_connection(self.hass, connection)) is None:
                return self._create_entry(connection)
            errors["base"] = error
            self._defaults = connection

        return self.async_show_form(
            step_id="connection",
            data_schema=connection_schema(self._defaults),
            errors=errors,
            description_placeholders={"entity_id": self._source[CONF_SOURCE_ENTITY_ID]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the connection to Kodi."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults = {key: entry.data.get(key) for key in CONNECTION_KEYS}
        if user_input is not None:
            connection = normalize_connection(user_input)
            if (error := await validate_connection(self.hass, connection)) is None:
                return self.async_update_reload_and_abort(entry, data_updates=connection)
            errors["base"] = error
            defaults = connection

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=connection_schema(defaults),
            errors=errors,
        )

    def _create_entry(self, connection: dict[str, Any]) -> ConfigFlowResult:
        entity_id = self._source[CONF_SOURCE_ENTITY_ID]
        if self._source[CONF_HIDE_SOURCE]:
            registry = er.async_get(self.hass)
            source = registry.async_get(entity_id)
            if source is not None and source.hidden_by is None:
                registry.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
        state = self.hass.states.get(entity_id)
        title = state.name if state is not None else entity_id
        return self.async_create_entry(title=title, data={**self._source, **connection})

    @callback
    def _configured_sources(self) -> list[str]:
        registry = er.async_get(self.hass)
        return [
            entity_id
            for entry in self._async_current_entries(include_ignore=False)
            if (
                entity_id := er.async_resolve_entity_id(
                    registry, entry.data[CONF_SOURCE_ENTITY_UUID]
                )
            )
        ]
