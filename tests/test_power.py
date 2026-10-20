"""Tests for switching Kodi on (Wake on LAN) and off (Kodi's power commands)."""

import asyncio
from unittest.mock import patch

import pytest

from custom_components.better_kodi.const import (
    CONF_MAC,
    CONF_POWER_OFF,
    CONF_POWER_ON,
    CONF_WAKE_TIMEOUT,
    DEFAULT_OPTIONS,
    POWER_ON_WAKE_ON_LAN,
)
from custom_components.better_kodi.kodi_client import KodiConnectionError
from custom_components.better_kodi.media_player import BetterKodiMediaPlayer
from custom_components.better_kodi.power import magic_packet, normalize_mac
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DATA_COMPONENT,
    DOMAIN as MP_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import KodiSource, own_entity_id, setup_better_kodi
from .fake_kodi import FakeKodi

FILE = "https://cdn.example/a.mp4"
MAC = "AA:BB:CC:DD:EE:FF"
SEND = "custom_components.better_kodi.media_player.async_send_magic_packet"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", MAC),
        ("AA-BB-CC-DD-EE-FF", MAC),
        ("aabb.ccdd.eeff", MAC),
        (" aabbccddeeff ", MAC),
        ("Busy", None),
        ("00:00:00:00:00:00", None),
        ("aa:bb:cc:dd:ee", None),
        (None, None),
    ],
)
def test_normalize_mac(text: str | None, expected: str | None) -> None:
    assert normalize_mac(text) == expected


def test_magic_packet() -> None:
    packet = magic_packet("aa-bb-cc-dd-ee-ff")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16
    with pytest.raises(ValueError, match="not a MAC address"):
        magic_packet("nope")


def set_options(hass: HomeAssistant, entry: ConfigEntry, **options: object) -> None:
    hass.config_entries.async_update_entry(entry, options={**DEFAULT_OPTIONS, **options})


@pytest.fixture
async def suspended(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> tuple[ConfigEntry, str, FakeKodi]:
    """Kodi is suspended; Better Kodi wakes it with Wake on LAN. No automation exists."""
    kodi = fakes[kodi_source.host]
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_ON: POWER_ON_WAKE_ON_LAN, CONF_MAC: MAC})
    kodi.offline = True
    hass.states.async_set(kodi_source.entity_id, "off", {"supported_features": 384})
    return entry, own_entity_id(hass, entry), kodi


def wakes_after(hass: HomeAssistant, source: KodiSource, kodi: FakeKodi, packets: int):
    """Record magic packets; Kodi wakes after the given number of them."""
    sent: list[tuple[str, str]] = []

    async def send(hass_: HomeAssistant, mac: str, address: str) -> None:
        sent.append((mac, address))
        if len(sent) == packets:

            @callback
            def wake() -> None:
                kodi.offline = False
                hass.states.async_set(source.entity_id, "idle", {"supported_features": 384})

            hass.loop.call_later(0.01, wake)

    return sent, patch(SEND, send)


async def play(hass: HomeAssistant, player: str) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        "play_media",
        {
            ATTR_ENTITY_ID: player,
            ATTR_MEDIA_CONTENT_ID: FILE,
            ATTR_MEDIA_CONTENT_TYPE: "url",
            "enqueue": "add",
        },
        blocking=True,
    )


async def test_wake_on_lan_before_sending(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    _, player, kodi = suspended
    events: list[str] = []
    hass.bus.async_listen("kodi.turn_on", callback(lambda event: events.append("automation")))
    sent, patcher = wakes_after(hass, kodi_source, kodi, 1)
    with patcher:
        await play(hass, player)
    assert sent == [(MAC, "255.255.255.255")]
    assert events == []  # the Kodi integration's turn-on (automation) is not used
    assert kodi.files() == [FILE]


async def test_wake_on_lan_repeats_lost_packets(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    _, player, kodi = suspended
    sent, patcher = wakes_after(hass, kodi_source, kodi, 3)
    with patcher, patch("custom_components.better_kodi.media_player.WAKE_ON_LAN_REPEAT", 0.02):
        await play(hass, player)
    assert len(sent) == 3
    assert kodi.files() == [FILE]


async def test_wake_on_lan_times_out(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    entry, player, kodi = suspended
    set_options(
        hass,
        entry,
        **{CONF_POWER_ON: POWER_ON_WAKE_ON_LAN, CONF_MAC: MAC, CONF_WAKE_TIMEOUT: 0.05},
    )
    sent, patcher = wakes_after(hass, kodi_source, kodi, 1000)
    with patcher, pytest.raises(HomeAssistantError) as err:
        await play(hass, player)
    assert err.value.translation_key == "wake_on_lan_timeout"
    assert sent
    assert kodi.files() == []


async def test_concurrent_sends_share_one_wake(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    _, player, kodi = suspended
    sent, patcher = wakes_after(hass, kodi_source, kodi, 1)
    with patcher:
        await asyncio.gather(play(hass, player), play(hass, player))
    assert len(sent) == 1
    assert kodi.files() == [FILE, FILE]


def entity_of(hass: HomeAssistant, entity_id: str) -> BetterKodiMediaPlayer:
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert isinstance(entity, BetterKodiMediaPlayer)
    return entity


def record_forwarded(hass: HomeAssistant, service: str) -> list[str]:
    """Stand in for the Kodi integration's service; returns the targeted entities."""
    targets: list[str] = []

    async def handler(call: ServiceCall) -> None:
        entity_ids = call.data[ATTR_ENTITY_ID]
        targets.extend([entity_ids] if isinstance(entity_ids, str) else entity_ids)

    hass.services.async_register(MP_DOMAIN, service, handler)
    return targets


async def test_turn_on_sends_wake_on_lan(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    _, player, kodi = suspended
    forwarded = record_forwarded(hass, "turn_on")
    sent, patcher = wakes_after(hass, kodi_source, kodi, 1)
    with patcher:
        await entity_of(hass, player).async_turn_on()
        await asyncio.sleep(0.05)  # let the simulated Kodi wake up (no lingering timer)
    assert sent == [(MAC, "255.255.255.255")]
    assert forwarded == []


async def test_turn_on_without_mac(hass: HomeAssistant, suspended) -> None:
    entry, player, _ = suspended
    set_options(hass, entry, **{CONF_POWER_ON: POWER_ON_WAKE_ON_LAN, CONF_MAC: ""})
    with pytest.raises(ServiceValidationError) as err:
        await entity_of(hass, player).async_turn_on()
    assert err.value.translation_key == "no_mac"


async def test_turn_on_with_automation_is_forwarded(
    hass: HomeAssistant, kodi_source: KodiSource
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    forwarded = record_forwarded(hass, "turn_on")
    with patch(SEND) as send:
        await entity_of(hass, own_entity_id(hass, entry)).async_turn_on()
    send.assert_not_called()
    assert forwarded == [kodi_source.entity_id]


@pytest.mark.parametrize(
    ("mode", "method"),
    [
        ("suspend", "System.Suspend"),
        ("hibernate", "System.Hibernate"),
        ("shutdown", "System.Shutdown"),
        ("quit", "Application.Quit"),
    ],
)
async def test_turn_off_with_kodi_command(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    fakes: dict[str, FakeKodi],
    mode: str,
    method: str,
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_OFF: mode})
    kodi = fakes[kodi_source.host]
    forwarded = record_forwarded(hass, "turn_off")
    await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()
    assert method in kodi.method_calls()
    assert forwarded == []


async def test_turn_off_not_allowed(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_OFF: "suspend"})
    kodi = fakes[kodi_source.host]
    kodi.power["cansuspend"] = False
    with pytest.raises(HomeAssistantError) as err:
        await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()
    assert err.value.translation_key == "power_off_not_allowed"
    assert "System.Suspend" not in kodi.method_calls()


async def test_turn_off_when_already_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_OFF: "suspend"})
    fakes[kodi_source.host].offline = True
    hass.states.async_set(kodi_source.entity_id, "off", {"supported_features": 384})
    await hass.async_block_till_done()
    await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()


async def test_turn_off_survives_kodi_going_down(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Kodi may drop the connection before it answers the suspend command."""
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_OFF: "suspend"})
    kodi = fakes[kodi_source.host]

    def suspend_and_drop() -> str:
        raise KodiConnectionError("connection closed")

    kodi._System_Suspend = suspend_and_drop  # type: ignore[method-assign]
    await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()
    assert "System.Suspend" in kodi.method_calls()


async def test_turn_off_with_automation_is_forwarded(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    forwarded = record_forwarded(hass, "turn_off")
    await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()
    assert "System.Suspend" not in fakes[kodi_source.host].method_calls()
    assert forwarded == [kodi_source.entity_id]


async def test_first_wake_on_lan_failure_is_reported(hass: HomeAssistant, suspended) -> None:
    _, player, kodi = suspended

    async def fail(*args: object) -> None:
        raise OSError("Network is unreachable")

    with patch(SEND, fail), pytest.raises(HomeAssistantError) as err:
        await play(hass, player)
    assert err.value.translation_key == "wake_on_lan_failed"
    assert kodi.files() == []


async def test_later_wake_on_lan_failure_is_retried(
    hass: HomeAssistant, suspended, kodi_source: KodiSource
) -> None:
    _, player, kodi = suspended
    attempts: list[int] = []

    async def flaky(hass_: HomeAssistant, mac: str, address: str) -> None:
        attempts.append(1)
        if len(attempts) == 2:
            raise OSError("Network is unreachable")
        if len(attempts) == 3:
            kodi.offline = False
            hass.states.async_set(kodi_source.entity_id, "idle", {"supported_features": 384})

    with (
        patch(SEND, flaky),
        patch("custom_components.better_kodi.media_player.WAKE_ON_LAN_REPEAT", 0.01),
    ):
        await play(hass, player)
    assert len(attempts) == 3
    assert kodi.files() == [FILE]


async def test_quit_when_kodi_is_unreachable(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    """Not reported as done when Kodi never got the command."""
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_POWER_OFF: "quit"})
    fakes[kodi_source.host].offline = True
    with pytest.raises(HomeAssistantError) as err:
        await entity_of(hass, own_entity_id(hass, entry)).async_turn_off()
    assert err.value.translation_key == "cannot_connect"
