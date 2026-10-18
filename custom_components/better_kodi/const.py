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

CONF_WAKE: Final = "wake_on_send"
CONF_WAKE_TIMEOUT: Final = "wake_timeout"
DEFAULT_WAKE_TIMEOUT: Final = 120
READY_TIMEOUT: Final = 30  # after the Kodi entity is no longer "off"

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
