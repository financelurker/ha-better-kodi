# Based on homeassistant/components/kodi from Home Assistant Core 2026.10.2
# (Apache-2.0). Modified for Better Kodi; see NOTICE and the git history.
"""Constants for the Kodi integration."""

DOMAIN = "better_kodi"

CONF_WS_PORT = "ws_port"

DEFAULT_PORT = 8080
DEFAULT_SSL = False
DEFAULT_TIMEOUT = 5
DEFAULT_WS_PORT = 9090

EVENT_TURN_OFF = "better_kodi.turn_off"
EVENT_TURN_ON = "better_kodi.turn_on"
