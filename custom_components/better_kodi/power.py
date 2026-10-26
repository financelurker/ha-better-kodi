"""Switching Kodi on (Wake on LAN) and off (Kodi's own power commands)."""

import asyncio
from collections.abc import Callable
import logging
import re
import socket

from homeassistant.core import HomeAssistant

from .const import WAKE_ON_LAN_PORT
from .kodi_client import KodiClient, KodiConnectionError, KodiError

_LOGGER = logging.getLogger(__name__)

# Kodi only does these if the system allows it (for example logind on Linux).
POWER_CAPABILITIES = {
    "System.Suspend": "cansuspend",
    "System.Hibernate": "canhibernate",
    "System.Shutdown": "canshutdown",
}

MAC_DIGITS = re.compile(r"^[0-9a-f]{12}$")
MAC_READ_ATTEMPTS = 3
MAC_READ_DELAY = 0.5


def normalize_mac(text: str | None) -> str | None:
    """Return a MAC address as AA:BB:CC:DD:EE:FF, or None if it isn't one."""
    digits = re.sub(r"[\s:.\-]", "", text or "").lower()
    if not MAC_DIGITS.match(digits) or digits == "0" * 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()


def magic_packet(mac: str) -> bytes:
    """Return the Wake on LAN packet: six 0xFF bytes, then the MAC 16 times."""
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(normalized.replace(":", "")) * 16


def _send(packet: bytes, address: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (address, port))


async def async_send_magic_packet(
    hass: HomeAssistant, mac: str, address: str, port: int = WAKE_ON_LAN_PORT
) -> None:
    """Send one Wake on LAN packet (in the executor: the address may need a lookup)."""
    packet = magic_packet(mac)
    await hass.async_add_executor_job(_send, packet, address, port)
    _LOGGER.debug("Sent Wake on LAN packet for %s to %s:%s", mac, address, port)


class PowerOffNotAllowed(Exception):
    """Kodi says it may not suspend, hibernate or shut down this device."""


async def async_power_off(
    client: KodiClient, method: str, *, still_wanted: Callable[[], bool] | None = None
) -> None:
    """Switch Kodi off with one of its JSON-RPC power commands.

    still_wanted is asked right before the command; False leaves Kodi on.
    """
    # Fails if Kodi is unreachable, so that a dropped answer below means "going down".
    await client.ping()
    if capability := POWER_CAPABILITIES.get(method):
        properties = await client.call("System.GetProperties", {"properties": [capability]})
        if not (properties or {}).get(capability):
            raise PowerOffNotAllowed(method)
    if still_wanted is not None and not still_wanted():
        return
    try:
        await client.call(method)
    except KodiConnectionError as err:
        # Kodi may go down before its answer arrives.
        _LOGGER.debug("No answer to %s, Kodi is probably going down: %s", method, err)


async def async_read_mac(client: KodiClient) -> str | None:
    """Ask a running Kodi for its MAC address.

    Kodi answers "Busy" while it is still collecting network information, so
    the question is repeated a few times.
    """
    for attempt in range(MAC_READ_ATTEMPTS):
        try:
            labels = await client.call("XBMC.GetInfoLabels", {"labels": ["Network.MacAddress"]})
        except KodiError as err:
            _LOGGER.debug("Could not read Kodi's MAC address: %s", err)
            return None
        if mac := normalize_mac((labels or {}).get("Network.MacAddress")):
            return mac
        if attempt + 1 < MAC_READ_ATTEMPTS:
            await asyncio.sleep(MAC_READ_DELAY)
    return None
