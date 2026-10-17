"""Tests for the Better Kodi config flow."""

from unittest.mock import patch

import pytest

from custom_components.better_kodi.const import (
    CONF_HIDE_SOURCE,
    CONF_SOURCE_ENTITY_ID,
    CONF_SOURCE_ENTITY_UUID,
    CONF_WS_PORT,
    DOMAIN,
)
from custom_components.better_kodi.kodi_client import KodiAuthError, KodiClient
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from .conftest import KodiSource, better_kodi_entry
from .fake_kodi import FakeKodi


def schema_defaults(result) -> dict:
    """Return defaults and suggested values of a form."""
    values = {}
    for key in result["data_schema"].schema:
        if (suggested := (key.description or {}).get("suggested_value")) is not None:
            values[str(key)] = suggested
        elif callable(getattr(key, "default", None)):
            values[str(key)] = key.default()
    return values


async def start_flow(hass: HomeAssistant, source: KodiSource, *, hide: bool = True):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY_ID: source.entity_id, CONF_HIDE_SOURCE: hide}
    )


async def test_full_flow_prefills_and_hides(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    result = await start_flow(hass, kodi_source)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"
    defaults = schema_defaults(result)
    assert defaults[CONF_HOST] == "10.0.0.5"
    assert defaults[CONF_PORT] == 8080
    assert defaults[CONF_WS_PORT] == 9090
    assert defaults[CONF_USERNAME] == "kodi"
    assert defaults[CONF_PASSWORD] == "secret"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "10.0.0.5",
            CONF_PORT: 8080.0,
            CONF_WS_PORT: 9090.0,
            CONF_USERNAME: "kodi",
            CONF_PASSWORD: "secret",
            CONF_SSL: False,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wohnzimmer Kodi"
    assert result["result"].unique_id == kodi_source.uuid
    assert result["data"] == {
        CONF_SOURCE_ENTITY_ID: kodi_source.entity_id,
        CONF_SOURCE_ENTITY_UUID: kodi_source.uuid,
        CONF_HIDE_SOURCE: True,
        CONF_HOST: "10.0.0.5",
        CONF_PORT: 8080,
        CONF_WS_PORT: 9090,
        CONF_USERNAME: "kodi",
        CONF_PASSWORD: "secret",
        CONF_SSL: False,
    }
    source = er.async_get(hass).async_get(kodi_source.entity_id)
    assert source.hidden_by is er.RegistryEntryHider.INTEGRATION


async def test_flow_without_hiding(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    result = await start_flow(hass, kodi_source, hide=False)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_SSL: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WS_PORT] is None
    assert er.async_get(hass).async_get(kodi_source.entity_id).hidden_by is None


async def test_user_hidden_source_stays_user_hidden(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    er.async_get(hass).async_update_entity(
        kodi_source.entity_id, hidden_by=er.RegistryEntryHider.USER
    )
    result = await start_flow(hass, kodi_source)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_SSL: False}
    )
    await hass.async_block_till_done()
    source = er.async_get(hass).async_get(kodi_source.entity_id)
    assert source.hidden_by is er.RegistryEntryHider.USER


async def test_not_a_kodi_entity(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    other = er.async_get(hass).async_get_or_create("media_player", "cast", "tv")
    hass.states.async_set(other.entity_id, "idle")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY_ID: other.entity_id, CONF_HIDE_SOURCE: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCE_ENTITY_ID: "not_kodi"}


async def test_cannot_connect_then_recover(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    result = await start_flow(hass, kodi_source)
    fakes[kodi_source.host].offline = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_SSL: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    fakes[kodi_source.host].offline = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_SSL: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_auth(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    result = await start_flow(hass, kodi_source)
    with patch.object(KodiClient, "ping", side_effect=KodiAuthError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "10.0.0.5", CONF_PORT: 8080, CONF_SSL: False}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_already_configured_source_is_not_offered(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    better_kodi_entry(kodi_source).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SOURCE_ENTITY_ID: kodi_source.entity_id, CONF_HIDE_SOURCE: True},
        )


async def test_reconfigure(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fakes["10.0.0.9"] = FakeKodi()

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert schema_defaults(result)[CONF_HOST] == "10.0.0.5"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "10.0.0.9", CONF_PORT: 8081, CONF_WS_PORT: 9091, CONF_SSL: False},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "10.0.0.9"
    assert entry.data[CONF_PORT] == 8081
    assert entry.data[CONF_SOURCE_ENTITY_UUID] == kodi_source.uuid


async def test_reauth(hass: HomeAssistant, kodi_source: KodiSource) -> None:
    entry = better_kodi_entry(kodi_source)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch.object(KodiClient, "ping", side_effect=KodiAuthError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "kodi", CONF_PASSWORD: "wrong"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "kodi", CONF_PASSWORD: "new"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
