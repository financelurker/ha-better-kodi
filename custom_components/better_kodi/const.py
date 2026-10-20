"""Constants for Better Kodi."""

from typing import Final

DOMAIN: Final = "better_kodi"
KODI_DOMAIN: Final = "kodi"

# Config entry data
CONF_SOURCE_ENTITY_ID: Final = "source_entity_id"
CONF_SOURCE_ENTITY_UUID: Final = "source_entity_uuid"
CONF_HIDE_SOURCE: Final = "hide_source"
CONF_WS_PORT: Final = "ws_port"
CONF_ASSIST_MIGRATED: Final = "assist_migrated"

# Options
CONF_POWER_ON: Final = "power_on"
CONF_MAC: Final = "mac"
CONF_BROADCAST: Final = "broadcast_address"
CONF_POWER_OFF: Final = "power_off"
CONF_WAKE: Final = "wake_on_send"
CONF_WAKE_TIMEOUT: Final = "wake_timeout"
CONF_BROWSE_CHOICES: Final = "media_browser_choices"
CONF_LINK_ENTITIES: Final = "link_entities"
CONF_ONLINE_LOOKUPS: Final = "online_lookups"

# How Better Kodi switches Kodi on: the Kodi integration's "turn on", which
# only runs the user's "Kodi turned on" automation, or Wake on LAN itself.
POWER_ON_AUTOMATION: Final = "automation"
POWER_ON_WAKE_ON_LAN: Final = "wake_on_lan"
POWER_ON_MODES: Final = (POWER_ON_AUTOMATION, POWER_ON_WAKE_ON_LAN)

# How Better Kodi switches Kodi off -> Kodi JSON-RPC method (None: automation)
POWER_OFF_AUTOMATION: Final = "automation"
POWER_OFF_METHODS: Final[dict[str, str | None]] = {
    POWER_OFF_AUTOMATION: None,
    "suspend": "System.Suspend",
    "hibernate": "System.Hibernate",
    "shutdown": "System.Shutdown",
    "quit": "Application.Quit",
}

DEFAULT_BROADCAST: Final = "255.255.255.255"
DEFAULT_WAKE_TIMEOUT: Final = 120
WAKE_ON_LAN_PORT: Final = 9
WAKE_ON_LAN_REPEAT: Final = 10  # seconds between magic packets while waking
READY_TIMEOUT: Final = 30  # after the Kodi entity is no longer "off"

DEFAULT_OPTIONS: Final = {
    CONF_POWER_ON: POWER_ON_AUTOMATION,
    CONF_MAC: "",
    CONF_BROADCAST: DEFAULT_BROADCAST,
    CONF_POWER_OFF: POWER_OFF_AUTOMATION,
    CONF_WAKE: True,
    CONF_WAKE_TIMEOUT: DEFAULT_WAKE_TIMEOUT,
    CONF_BROWSE_CHOICES: True,
    CONF_LINK_ENTITIES: True,
    CONF_ONLINE_LOOKUPS: True,
}

DEFAULT_PORT: Final = 8080
DEFAULT_WS_PORT: Final = 9090

# Kodi playlist IDs
VIDEO_PLAYLIST: Final = 1

# Media browser
QUEUE_CONTENT_TYPE: Final = "better_kodi_queue"
QUEUE_ITEM_CONTENT_TYPE: Final = "better_kodi_queue_item"
QUEUE_CONTENT_ID: Final = "better_kodi://queue"
QUEUE_ITEM_PREFIX: Final = "better_kodi://queue/"

# Kodi library types that can be added to a playlist by ID
LIBRARY_ID_KEYS: Final = {
    "movie": "movieid",
    "episode": "episodeid",
    "musicvideo": "musicvideoid",
}

# Services
SERVICE_CALL_METHOD: Final = "call_method"
SERVICE_GET_QUEUE: Final = "get_queue"
SERVICE_REMOVE_FROM_QUEUE: Final = "remove_from_queue"
SERVICE_MOVE_QUEUE_ITEM: Final = "move_queue_item"
SERVICE_HANDOVER: Final = "handover"

ATTR_METHOD: Final = "method"
ATTR_PARAMS: Final = "params"
ATTR_POSITION: Final = "position"
ATTR_TO_POSITION: Final = "to_position"
ATTR_DESTINATION: Final = "destination"
ATTR_KEEP_SOURCE_PLAYING: Final = "keep_source_playing"
ATTR_QUEUE_POSITION: Final = "queue_position"
ATTR_QUEUE_LENGTH: Final = "queue_length"
