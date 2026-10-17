"""Better Kodi media player: mirrors a Kodi entity and adds queue features."""

from collections.abc import Awaitable
from datetime import datetime
import logging
import re
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_BROWSE_MEDIA,
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PLAY_PAUSE,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_SEEK,
    SERVICE_MEDIA_STOP,
    SERVICE_SHUFFLE_SET,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)

from . import BetterKodiConfigEntry, BetterKodiData
from .const import (
    ATTR_QUEUE_LENGTH,
    ATTR_QUEUE_POSITION,
    CONF_ASSIST_MIGRATED,
    CONF_SOURCE_ENTITY_UUID,
    DOMAIN,
    LIBRARY_ID_KEYS,
    QUEUE_CONTENT_ID,
    QUEUE_CONTENT_TYPE,
    QUEUE_ITEM_CONTENT_TYPE,
    QUEUE_ITEM_PREFIX,
)
from .kodi_client import KodiError, KodiRpcError
from .queue import EnqueueMode, HandoverSnapshot, QueueError, QueueStatus
from .resolvers import ResolveContext, ResolvedMedia, ResolveError, async_resolve

_LOGGER = logging.getLogger(__name__)

OWN_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
)

LINK_IN_TEXT = re.compile(r"https?://\S+")
URL_SCHEMES = ("http://", "https://", "plugin://", "special://", "smb://", "nfs://")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterKodiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the Better Kodi media player."""
    async_add_entities([BetterKodiMediaPlayer(hass, entry)])


class BetterKodiMediaPlayer(MediaPlayerEntity):
    """A Kodi media player with queue, URL casting and handover."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_translation_key = "better_kodi"

    def __init__(self, hass: HomeAssistant, entry: BetterKodiConfigEntry) -> None:
        """Initialize the wrapper for the Kodi entity of a config entry."""
        self._entry = entry
        self._data: BetterKodiData = entry.runtime_data
        self._source_uuid: str = entry.data[CONF_SOURCE_ENTITY_UUID]
        self._source_entity_id = (
            er.async_resolve_entity_id(er.async_get(hass), self._source_uuid) or ""
        )
        self._source_state: State | None = None
        self._queue_status = QueueStatus(None, None, 0)
        self._attr_unique_id = entry.entry_id
        self.device_entry = async_entity_id_to_device(hass, self._source_entity_id)
        self._queue_refresher: Debouncer[Any] | None = None

    # --- Lifecycle ----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Start mirroring the Kodi entity."""
        self._queue_refresher = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=1.0,
            immediate=True,
            function=self._async_refresh_queue,
        )
        self.async_on_remove(self._queue_refresher.async_shutdown)
        self._source_state = self.hass.states.get(self._source_entity_id)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_entity_id], self._on_source_state
            )
        )
        self.async_on_remove(
            async_track_entity_registry_updated_event(
                self.hass, self._source_entity_id, self._on_source_registry_update
            )
        )
        self._data.queue_listeners.append(self._schedule_queue_refresh)
        self.async_on_remove(
            lambda: self._data.queue_listeners.remove(self._schedule_queue_refresh)
        )
        self._async_migrate_assist_settings()
        await self._async_refresh_queue()

    @callback
    def _on_source_state(self, event: Event[EventStateChangedData]) -> None:
        old = self._source_state
        self._source_state = event.data["new_state"]
        new = self._source_state
        if old is None or new is None or old.state != new.state:
            self._schedule_queue_refresh()
        self.async_write_ha_state()

    @callback
    def _on_source_registry_update(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Reload when the Kodi entity was renamed or removed."""
        if event.data["action"] == "remove" or (
            event.data["action"] == "update" and "entity_id" in event.data["changes"]
        ):
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    @callback
    def _schedule_queue_refresh(self) -> None:
        if self._queue_refresher is not None:
            self._queue_refresher.async_schedule_call()

    async def _async_refresh_queue(self) -> None:
        try:
            status = await self._data.queue.status()
        except KodiError as err:
            _LOGGER.debug("Could not read the queue of %s: %s", self.entity_id, err)
            status = QueueStatus(None, None, 0)
        if status != self._queue_status:
            self._queue_status = status
            if self.hass is not None and self.entity_id:
                self.async_write_ha_state()

    @callback
    def _async_migrate_assist_settings(self) -> None:
        """Move Assist exposure and aliases from the Kodi entity, once."""
        if self._entry.data.get(CONF_ASSIST_MIGRATED) or not self._source_entity_id:
            return
        for assistant, settings in exposed_entities.async_get_entity_settings(
            self.hass, self._source_entity_id
        ).items():
            if (should_expose := settings.get("should_expose")) is None:
                continue
            exposed_entities.async_expose_entity(
                self.hass, assistant, self.entity_id, should_expose
            )
            exposed_entities.async_expose_entity(
                self.hass, assistant, self._source_entity_id, False
            )
        registry = er.async_get(self.hass)
        source = registry.async_get(self._source_entity_id)
        own = registry.async_get(self.entity_id)
        if source is not None and own is not None:
            extra = [alias for alias in source.aliases if alias not in own.aliases]
            if extra:
                registry.async_update_entity(self.entity_id, aliases=[*own.aliases, *extra])
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_ASSIST_MIGRATED: True}
        )

    # --- Mirrored state -------------------------------------------------------

    def _src(self, attribute: str) -> Any:
        return self._source_state.attributes.get(attribute) if self._source_state else None

    @property
    def available(self) -> bool:
        """Available while the Kodi entity is."""
        return self._source_state is not None and self._source_state.state != STATE_UNAVAILABLE

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the Kodi entity's state."""
        if self._source_state is None or self._source_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return None
        try:
            return MediaPlayerState(self._source_state.state)
        except ValueError:
            return None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return the Kodi entity's features plus Better Kodi's."""
        return MediaPlayerEntityFeature(self._src("supported_features") or 0) | OWN_FEATURES

    @property
    def volume_level(self) -> float | None:
        """Volume level (0..1)."""
        return self._src("volume_level")

    @property
    def is_volume_muted(self) -> bool | None:
        """Return True if muted."""
        return self._src("is_volume_muted")

    @property
    def media_content_id(self) -> str | None:
        """Content ID of the playing media."""
        return self._src("media_content_id")

    @property
    def media_content_type(self) -> str | None:
        """Content type of the playing media."""
        return self._src("media_content_type")

    @property
    def media_duration(self) -> int | None:
        """Duration in seconds."""
        return self._src("media_duration")

    @property
    def media_position(self) -> int | None:
        """Position in seconds."""
        return self._src("media_position")

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was last updated."""
        return self._src("media_position_updated_at")

    @property
    def media_title(self) -> str | None:
        """Title of the playing media."""
        return self._src("media_title")

    @property
    def media_artist(self) -> str | None:
        """Artist of the playing media."""
        return self._src("media_artist")

    @property
    def media_album_name(self) -> str | None:
        """Album of the playing media."""
        return self._src("media_album_name")

    @property
    def media_album_artist(self) -> str | None:
        """Album artist of the playing media."""
        return self._src("media_album_artist")

    @property
    def media_series_title(self) -> str | None:
        """Series title of the playing episode."""
        return self._src("media_series_title")

    @property
    def media_season(self) -> str | None:
        """Season of the playing episode."""
        return self._src("media_season")

    @property
    def media_episode(self) -> str | None:
        """Episode number of the playing episode."""
        return self._src("media_episode")

    @property
    def shuffle(self) -> bool | None:
        """Return True if shuffle is on."""
        return self._src("shuffle")

    @property
    def entity_picture(self) -> str | None:
        """Use the Kodi entity's picture (served by Home Assistant's proxy)."""
        return self._src("entity_picture")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Queue position and length, plus Kodi's dynamic range."""
        attributes: dict[str, Any] = {
            ATTR_QUEUE_POSITION: self._queue_status.position,
            ATTR_QUEUE_LENGTH: self._queue_status.length,
        }
        if (dynamic_range := self._src("dynamic_range")) is not None:
            attributes["dynamic_range"] = dynamic_range
        return attributes

    # --- Forwarded commands ---------------------------------------------------

    async def _forward(self, service: str, **data: Any) -> None:
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self._source_entity_id, **data},
            blocking=True,
            context=self._context,
        )

    async def async_turn_on(self) -> None:
        """Turn Kodi on (runs your Kodi turn-on automation)."""
        await self._forward(SERVICE_TURN_ON)

    async def async_turn_off(self) -> None:
        """Turn Kodi off (runs your Kodi turn-off automation)."""
        await self._forward(SERVICE_TURN_OFF)

    async def async_volume_up(self) -> None:
        """Raise the volume."""
        await self._forward(SERVICE_VOLUME_UP)

    async def async_volume_down(self) -> None:
        """Lower the volume."""
        await self._forward(SERVICE_VOLUME_DOWN)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume (0..1)."""
        await self._forward(SERVICE_VOLUME_SET, volume_level=volume)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        await self._forward(SERVICE_VOLUME_MUTE, is_volume_muted=mute)

    async def async_media_play(self) -> None:
        """Resume playback."""
        await self._forward(SERVICE_MEDIA_PLAY)

    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self._forward(SERVICE_MEDIA_PAUSE)

    async def async_media_play_pause(self) -> None:
        """Toggle play and pause."""
        await self._forward(SERVICE_MEDIA_PLAY_PAUSE)

    async def async_media_stop(self) -> None:
        """Stop playback."""
        await self._forward(SERVICE_MEDIA_STOP)

    async def async_media_next_track(self) -> None:
        """Play the next item of the queue."""
        await self._forward(SERVICE_MEDIA_NEXT_TRACK)

    async def async_media_previous_track(self) -> None:
        """Play the previous item of the queue."""
        await self._forward(SERVICE_MEDIA_PREVIOUS_TRACK)

    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        await self._forward(SERVICE_MEDIA_SEEK, seek_position=position)

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Turn shuffle on or off."""
        await self._forward(SERVICE_SHUFFLE_SET, shuffle=shuffle)

    # --- Playing and queueing ---------------------------------------------------

    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        enqueue: MediaPlayerEnqueue | None = None,
        **kwargs: Any,
    ) -> None:
        """Play or queue media, resolving page URLs (see docs/DESIGN.md)."""
        mode = EnqueueMode(enqueue) if enqueue else EnqueueMode.PLAY

        media_id = extract_link(media_id)
        if media_id.startswith(QUEUE_ITEM_PREFIX):
            position = _queue_position(media_id)
            await self._run(self._data.queue.play_position(position))
            return

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            url = async_process_play_media_url(self.hass, play_item.url)
            await self._run(self._data.queue.enqueue({"file": url}, mode))
            return

        if media_id.startswith(URL_SCHEMES):
            resolved = await self._async_resolve(media_id)
            if not resolved.queueable and mode in (EnqueueMode.NEXT, EnqueueMode.ADD):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="play_only",
                    translation_placeholders={"resolver": resolved.resolver},
                )
            self._data.queue.known.remember(resolved.file, resolved.title, resolved.thumbnail)
            await self._run(self._data.queue.enqueue(resolved.item, mode))
            return

        if (key := LIBRARY_ID_KEYS.get(str(media_type).lower())) and media_id.isdigit():
            await self._run(self._data.queue.enqueue({key: int(media_id)}, mode))
            return

        if mode in (EnqueueMode.NEXT, EnqueueMode.ADD):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="enqueue_not_supported",
                translation_placeholders={"media_type": str(media_type)},
            )
        await self._forward(
            "play_media",
            media_content_type=media_type,
            media_content_id=media_id,
        )

    async def async_clear_playlist(self) -> None:
        """Clear the video queue."""
        await self._run(self._data.queue.clear())

    async def _async_resolve(self, url: str) -> ResolvedMedia:
        addons = await self._data.async_get_addons()
        context = ResolveContext(session=self._data.client.session, addons=addons)
        try:
            return await async_resolve(url, context)
        except ResolveError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="resolve_failed",
                translation_placeholders={"url": url, "error": str(err)},
            ) from err

    async def _run[T](self, operation: Awaitable[T]) -> T:
        """Await a queue operation and turn Kodi errors into HA errors."""
        try:
            result = await operation
        except QueueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="queue_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except KodiRpcError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="kodi_error",
                translation_placeholders={"error": err.message},
            ) from err
        except KodiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"host": self._data.client.base_url},
            ) from err
        self._schedule_queue_refresh()
        return result

    # --- Services -------------------------------------------------------------

    async def async_call_method(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a Kodi JSON-RPC method and return its result."""
        return {"result": await self._run(self._data.client.call(method, params or None))}

    async def async_get_queue(self) -> dict[str, Any]:
        """Return the queue."""
        items = await self._run(self._data.queue.items())
        status = await self._run(self._data.queue.status())
        return {
            "position": status.position,
            "length": len(items),
            "items": [item.as_dict() for item in items],
        }

    async def async_remove_from_queue(self, position: int) -> None:
        """Remove an item from the queue."""
        await self._run(self._data.queue.remove(position))

    async def async_move_queue_item(self, position: int, to_position: int) -> None:
        """Move an item within the queue."""
        await self._run(self._data.queue.move(position, to_position))

    async def async_handover(
        self, target: str, keep_source_playing: bool = False, resume: bool = True
    ) -> None:
        """Continue the current playback on another Better Kodi entity."""
        target_data = self._target_data(target)
        snapshot: HandoverSnapshot = await self._run(self._data.queue.snapshot())
        try:
            await target_data.queue.restore(snapshot, resume=resume)
        except QueueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="queue_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except KodiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="handover_failed",
                translation_placeholders={"target": target, "error": str(err)},
            ) from err
        target_data.notify_queue_changed()
        if not keep_source_playing:
            await self.async_media_stop()

    def _target_data(self, target: str) -> BetterKodiData:
        registry = er.async_get(self.hass)
        target_entry = registry.async_get(target)
        if (
            target_entry is None
            or target_entry.platform != DOMAIN
            or target_entry.entity_id == self.entity_id
            or target_entry.config_entry_id is None
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_target",
                translation_placeholders={"target": target},
            )
        config_entry = self.hass.config_entries.async_get_entry(target_entry.config_entry_id)
        data = getattr(config_entry, "runtime_data", None)
        if not isinstance(data, BetterKodiData):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="target_not_ready",
                translation_placeholders={"target": target},
            )
        return data

    # --- Media browser ----------------------------------------------------------

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the Kodi library, with the queue as an extra folder."""
        if media_content_type == QUEUE_CONTENT_TYPE:
            return await self._async_browse_queue()

        data: dict[str, Any] = {ATTR_ENTITY_ID: self._source_entity_id}
        if media_content_type is not None:
            data[ATTR_MEDIA_CONTENT_TYPE] = media_content_type
        if media_content_id is not None:
            data[ATTR_MEDIA_CONTENT_ID] = media_content_id
        try:
            response = await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_BROWSE_MEDIA,
                data,
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            raise BrowseError(str(err)) from err
        payload = (response or {}).get(self._source_entity_id)
        if payload is None:
            raise BrowseError(f"Kodi returned nothing for {media_content_id}")
        result = browse_media_from(payload)
        if media_content_type is None and media_content_id is None:
            result.children = [
                await self._async_browse_queue(as_child=True),
                *(result.children or []),
            ]
        return result

    async def _async_browse_queue(self, *, as_child: bool = False) -> BrowseMedia:
        title = "Warteschlange" if self.hass.config.language.startswith("de") else "Queue"
        if as_child:
            return BrowseMedia(
                media_class=MediaClass.PLAYLIST,
                media_content_id=QUEUE_CONTENT_ID,
                media_content_type=QUEUE_CONTENT_TYPE,
                title=title,
                can_play=False,
                can_expand=True,
            )
        try:
            items = await self._data.queue.items()
        except KodiError as err:
            raise BrowseError(str(err)) from err
        children = [
            BrowseMedia(
                media_class=MediaClass.VIDEO,
                media_content_id=f"{QUEUE_ITEM_PREFIX}{item.position}",
                media_content_type=QUEUE_ITEM_CONTENT_TYPE,
                title=f"▶ {item.title}" if item.current else item.title,
                can_play=True,
                can_expand=False,
                thumbnail=self._queue_thumbnail(item.position, item.thumbnail),
            )
            for item in items
        ]
        return BrowseMedia(
            media_class=MediaClass.PLAYLIST,
            media_content_id=QUEUE_CONTENT_ID,
            media_content_type=QUEUE_CONTENT_TYPE,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    def _queue_thumbnail(self, position: int, thumbnail: str | None) -> str | None:
        if not thumbnail:
            return None
        if thumbnail.startswith(("http://", "https://")):
            return thumbnail
        return self.get_browse_image_url(
            QUEUE_ITEM_CONTENT_TYPE, str(position), media_image_id=thumbnail
        )

    async def async_get_browse_image(
        self,
        media_content_type: MediaType | str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Fetch a queue thumbnail from Kodi."""
        if not media_image_id:
            return None, None
        return await self._data.client.fetch_image(media_image_id)


def extract_link(text: str) -> str:
    """Return the first link in shared text ("Look at this: https://…"), else the text."""
    text = text.strip()
    if " " not in text and "\n" not in text:
        return text
    if match := LINK_IN_TEXT.search(text):
        return match.group(0)
    return text


def browse_media_from(payload: BrowseMedia | dict[str, Any]) -> BrowseMedia:
    """Rebuild a BrowseMedia tree from a browse_media action response."""
    if isinstance(payload, BrowseMedia):
        return payload
    children = [browse_media_from(child) for child in payload.get("children") or []]
    return BrowseMedia(
        media_class=payload.get("media_class") or MediaClass.DIRECTORY,
        media_content_id=payload.get("media_content_id") or "",
        media_content_type=payload.get("media_content_type") or "",
        title=payload.get("title") or "",
        can_play=bool(payload.get("can_play")),
        can_expand=bool(payload.get("can_expand")),
        children=children or None,
        children_media_class=payload.get("children_media_class"),
        thumbnail=payload.get("thumbnail"),
        not_shown=payload.get("not_shown") or 0,
    )


def _queue_position(media_id: str) -> int:
    try:
        return int(media_id.removeprefix(QUEUE_ITEM_PREFIX))
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="queue_error",
            translation_placeholders={"error": f"Unknown queue item {media_id}"},
        ) from err
