"""Config flow for Better Kodi."""

import asyncio
from collections.abc import Iterable, Mapping
import contextlib
import ipaddress
from typing import Any

import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er, selector

from . import client_from_entry_data
from .const import (
    CONF_BROADCAST,
    CONF_BROWSE_CHOICES,
    CONF_HIDE_SOURCE,
    CONF_LINK_ENTITIES,
    CONF_MAC,
    CONF_ONLINE_LOOKUPS,
    CONF_POWER_OFF,
    CONF_POWER_ON,
    CONF_SOURCE_ENTITY_ID,
    CONF_SOURCE_ENTITY_UUID,
    CONF_WAKE,
    CONF_WAKE_TIMEOUT,
    CONF_WS_PORT,
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DEFAULT_WS_PORT,
    DOMAIN,
    KODI_DOMAIN,
    POWER_OFF_METHODS,
    POWER_ON_MODES,
    POWER_ON_WAKE_ON_LAN,
)
from .kodi_client import KodiAuthError, KodiError
from .power import async_read_mac, normalize_mac

SECTION_POWER = "power"
SECTION_FEATURES = "features"
MAC_DETECT_TIMEOUT = 5
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the options flow."""
        return BetterKodiOptionsFlow()

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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Kodi rejected the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for Kodi's user name and password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            connection = {
                **entry.data,
                CONF_USERNAME: user_input.get(CONF_USERNAME),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD),
            }
            if (error := await validate_connection(self.hass, connection)) is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: connection[CONF_USERNAME],
                        CONF_PASSWORD: connection[CONF_PASSWORD],
                    },
                )
            errors["base"] = error

        username = entry.data.get(CONF_USERNAME)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Optional(
                        CONF_USERNAME,
                        **({"description": {"suggested_value": username}} if username else {}),
                    ): selector.TextSelector(),
                    probatio.Optional(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
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


class BetterKodiOptionsFlow(OptionsFlowWithReload):
    """Options: switching Kodi on and off, and which extras Better Kodi offers."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._detected_mac: str | None = None
        self._detected = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        errors: dict[str, str] = {}
        values = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            power = user_input.get(SECTION_POWER, {})
            values = {**values, **power, **user_input.get(SECTION_FEATURES, {})}
            mac_text = (power.get(CONF_MAC) or "").strip()  # an emptied field is left out
            mac = normalize_mac(mac_text)
            values[CONF_MAC] = mac or mac_text
            if mac_text and mac is None:
                errors["base"] = "invalid_mac"
            elif values[CONF_POWER_ON] == POWER_ON_WAKE_ON_LAN and mac is None:
                errors["base"] = "mac_required"
            values[CONF_BROADCAST] = (values.get(CONF_BROADCAST) or "").strip()
            if not errors and not _is_ipv4(values[CONF_BROADCAST]):
                errors["base"] = "invalid_broadcast"
            if not errors:
                values[CONF_WAKE_TIMEOUT] = int(values[CONF_WAKE_TIMEOUT])
                return self.async_create_entry(data={key: values[key] for key in DEFAULT_OPTIONS})
        elif not values[CONF_MAC]:
            values[CONF_MAC] = await self._async_detect_mac() or ""

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(values),
            errors=errors,
        )

    async def _async_detect_mac(self) -> str | None:
        """Ask Kodi for its MAC address once, if it is running."""
        if not self._detected:
            self._detected = True
            source_entity_id = er.async_resolve_entity_id(
                er.async_get(self.hass), self.config_entry.data[CONF_SOURCE_ENTITY_UUID]
            )
            source = self.hass.states.get(source_entity_id) if source_entity_id else None
            if source is None or source.state in (STATE_OFF, STATE_UNAVAILABLE):
                return None  # a suspended Kodi wouldn't answer; don't make the form wait
            client = client_from_entry_data(self.hass, dict(self.config_entry.data))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(MAC_DETECT_TIMEOUT):
                    self._detected_mac = await async_read_mac(client)
        return self._detected_mac

    @staticmethod
    def _schema(values: dict[str, Any]) -> probatio.Schema:
        def choice(key: str, modes: Iterable[str]) -> selector.SelectSelector:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(modes),
                    translation_key=key,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        mac = values.get(CONF_MAC)
        power = probatio.Schema(
            {
                probatio.Required(CONF_POWER_ON, default=values[CONF_POWER_ON]): choice(
                    CONF_POWER_ON, POWER_ON_MODES
                ),
                probatio.Optional(
                    CONF_MAC, **({"description": {"suggested_value": mac}} if mac else {})
                ): selector.TextSelector(),
                probatio.Required(
                    CONF_BROADCAST, default=values[CONF_BROADCAST]
                ): selector.TextSelector(),
                probatio.Required(CONF_POWER_OFF, default=values[CONF_POWER_OFF]): choice(
                    CONF_POWER_OFF, POWER_OFF_METHODS
                ),
                probatio.Required(CONF_WAKE, default=values[CONF_WAKE]): selector.BooleanSelector(),
                probatio.Required(
                    CONF_WAKE_TIMEOUT, default=values[CONF_WAKE_TIMEOUT]
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=900,
                        step=5,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        features = probatio.Schema(
            {
                probatio.Required(key, default=values[key]): selector.BooleanSelector()
                for key in (CONF_BROWSE_CHOICES, CONF_LINK_ENTITIES, CONF_ONLINE_LOOKUPS)
            }
        )
        return probatio.Schema(
            {
                probatio.Required(SECTION_POWER): section(power),
                probatio.Required(SECTION_FEATURES): section(features),
            }
        )


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text.strip())
    except ValueError:
        return False
    return True
