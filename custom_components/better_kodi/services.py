"""Better Kodi services."""

import probatio

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, service

from .const import (
    ATTR_DESTINATION,
    ATTR_KEEP_SOURCE_PLAYING,
    ATTR_METHOD,
    ATTR_PARAMS,
    ATTR_POSITION,
    ATTR_TO_POSITION,
    DOMAIN,
    SERVICE_CALL_METHOD,
    SERVICE_GET_QUEUE,
    SERVICE_HANDOVER,
    SERVICE_MOVE_QUEUE_ITEM,
    SERVICE_REMOVE_FROM_QUEUE,
)

ATTR_RESUME = "resume"
POSITION = probatio.All(probatio.Coerce(int), probatio.Range(min=0))


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the entity services of Better Kodi."""

    def register(
        name: str,
        schema: dict | None,
        func: str,
        supports_response: SupportsResponse = SupportsResponse.NONE,
    ) -> None:
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=MEDIA_PLAYER_DOMAIN,
            schema=schema,
            func=func,
            supports_response=supports_response,
        )

    register(
        SERVICE_CALL_METHOD,
        {
            probatio.Required(ATTR_METHOD): cv.string,
            probatio.Optional(ATTR_PARAMS): dict,
        },
        "async_call_method",
        SupportsResponse.OPTIONAL,
    )
    register(SERVICE_GET_QUEUE, {}, "async_get_queue", SupportsResponse.ONLY)
    register(
        SERVICE_REMOVE_FROM_QUEUE,
        {probatio.Required(ATTR_POSITION): POSITION},
        "async_remove_from_queue",
    )
    register(
        SERVICE_MOVE_QUEUE_ITEM,
        {
            probatio.Required(ATTR_POSITION): POSITION,
            probatio.Required(ATTR_TO_POSITION): POSITION,
        },
        "async_move_queue_item",
    )
    register(
        SERVICE_HANDOVER,
        {
            probatio.Required(ATTR_DESTINATION): cv.entity_id,
            probatio.Optional(ATTR_KEEP_SOURCE_PLAYING, default=False): cv.boolean,
            probatio.Optional(ATTR_RESUME, default=True): cv.boolean,
        },
        "async_handover",
    )
