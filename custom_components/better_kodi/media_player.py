"""Better Kodi media player: mirrors a Kodi entity and adds queue features."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from datetime import datetime
import logging
import re
import time
from typing import Any
from urllib.parse import quote, unquote

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DATA_COMPONENT,
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
    STATE_OFF,
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
from homeassistant.helpers.translation import async_get_translations

from . import BetterKodiConfigEntry, BetterKodiData, backends, get_option
from .announce import AnnounceError, ClipError, Outcome
from .assist import copy_aliases, transfer_exposure
from .const import (
    ATTR_QUEUE_LENGTH,
    ATTR_QUEUE_POSITION,
    CONF_ANNOUNCE_MAX_DURATION,
    CONF_ANNOUNCE_START_TIMEOUT,
    CONF_ANNOUNCE_VOLUME,
    CONF_ASSIST_MIGRATED,
    CONF_BROADCAST,
    CONF_BROWSE_CHOICES,
    CONF_MAC,
    CONF_ONLINE_LOOKUPS,
    CONF_POWER_OFF,
    CONF_POWER_ON,
    CONF_WAKE,
    CONF_WAKE_TIMEOUT,
    DOMAIN,
    LIBRARY_ID_KEYS,
    POWER_OFF_METHODS,
    POWER_ON_WAKE_ON_LAN,
    QUEUE_CONTENT_ID,
    QUEUE_CONTENT_TYPE,
    QUEUE_ITEM_CONTENT_TYPE,
    QUEUE_ITEM_PREFIX,
    READY_TIMEOUT,
    WAKE_ON_LAN_REPEAT,
)
from .kodi_client import KodiError, KodiRpcError
from .power import PowerOffNotAllowed, async_power_off, async_send_magic_packet
from .queue import EnqueueMode, HandoverSnapshot, QueueError, QueueStatus
from .resolvers import (
    KODI_PATH_SCHEMES,
    ResolveContext,
    ResolvedMedia,
    ResolveError,
    async_resolve,
)

_LOGGER = logging.getLogger(__name__)

OWN_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

CHOICES_PREFIX = "better_kodi://choose/"
ENQUEUE_PREFIX = "better_kodi://enqueue/"
CHOICE_ORDER = ("play", "next", "add", "replace")
READY_POLL_INTERVAL = 1.0
WAKE_TASKS = f"{DOMAIN}_wake_tasks"
KODI_EVENT_TURN_ON = "kodi.turn_on"  # fired by the Kodi integration's turn_on
LINK_IN_TEXT = re.compile(r"https?://\S+")
TRAILING_PUNCTUATION = ".,;:!?)]}>'\"»“”"
PLAYABLE_SCHEMES = frozenset({"http", "https", *KODI_PATH_SCHEMES})
ANNOUNCE_VOLUME = "announce_volume"  # in "extra" of play_media: 0..100


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
        self._source_entity_id = self._data.source_entity_id
        self._source_state: State | None = None
        self._queue_status = QueueStatus(None, None, 0)
        self._attr_unique_id = entry.entry_id
        self.device_entry = async_entity_id_to_device(hass, self._source_entity_id)
        self._queue_refresher: Debouncer[Any] | None = None
        self._choice_items: dict[str, BrowseMedia] = {}

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
        if _playing_item_changed(old, new):
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
        if self._entry.data.get(CONF_ASSIST_MIGRATED):
            return
        transfer_exposure(self.hass, self._source_entity_id, self.entity_id, unexpose_source=True)
        copy_aliases(self.hass, self._source_entity_id, self.entity_id)
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

    @contextlib.asynccontextmanager
    async def _command(self, *, uses_kodi: bool = True) -> AsyncIterator[None]:
        """Run a command after running announcements; they wait for it in turn."""
        if uses_kodi:
            self._data.mark_used()  # used from Home Assistant: don't switch off
        async with self._data.announcer.command():
            yield

    async def _forward(self, service: str, **data: Any) -> None:
        async with self._command():
            await self._call_source(service, **data)

    async def _call_source(self, service: str, **data: Any) -> None:
        """Call a service of the Kodi entity (not waiting for announcements)."""
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self._source_entity_id, **data},
            blocking=True,
            context=self._context,
        )

    @property
    def _wake_on_lan(self) -> bool:
        return get_option(self._entry, CONF_POWER_ON) == POWER_ON_WAKE_ON_LAN

    async def async_turn_on(self) -> None:
        """Turn Kodi on: Wake on LAN, or the Kodi integration's turn-on automation."""
        async with self._command():
            if self._wake_on_lan:
                await self._async_send_wake_on_lan()
            else:
                await self._call_source(SERVICE_TURN_ON)

    async def async_turn_off(self) -> None:
        """Turn Kodi off with the chosen Kodi command, or the turn-off automation."""
        async with self._command():
            await self._async_switch_off()

    async def _async_switch_off(self, still_wanted: Callable[[], bool] | None = None) -> None:
        mode = get_option(self._entry, CONF_POWER_OFF)
        if (method := POWER_OFF_METHODS.get(mode)) is None:
            await self._call_source(SERVICE_TURN_OFF)
            return
        if self.state == MediaPlayerState.OFF:
            return  # already off; Kodi wouldn't answer
        try:
            await self._run(async_power_off(self._data.client, method, still_wanted=still_wanted))
        except PowerOffNotAllowed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="power_off_not_allowed",
            ) from err

    async def _async_send_wake_on_lan(self) -> None:
        mac = get_option(self._entry, CONF_MAC)
        if not mac:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_mac")
        try:
            await async_send_magic_packet(self.hass, mac, get_option(self._entry, CONF_BROADCAST))
        except (OSError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_on_lan_failed",
                translation_placeholders={"error": str(err)},
            ) from err

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
        if kwargs.get(ATTR_MEDIA_ANNOUNCE):
            await self._async_announce(media_id, kwargs.get(ATTR_MEDIA_EXTRA) or {})
            return
        async with self._command():
            await self._async_play_media(media_type, media_id, enqueue)

    async def _async_play_media(
        self, media_type: MediaType | str, media_id: str, enqueue: MediaPlayerEnqueue | None
    ) -> None:
        mode = EnqueueMode(enqueue) if enqueue else EnqueueMode.PLAY
        media_id = extract_link(media_id)
        if media_id.startswith(ENQUEUE_PREFIX):
            # The choice in the ID wins over an "enqueue" passed with it.
            mode_text, _, encoded = media_id.removeprefix(ENQUEUE_PREFIX).partition("/")
            try:
                mode = EnqueueMode(mode_text)
            except ValueError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="queue_error",
                    translation_placeholders={"error": f"Unknown choice {mode_text!r}"},
                ) from err
            media_type, media_id = _decode(encoded)
        elif media_id.startswith(CHOICES_PREFIX):
            media_type, media_id = _decode(media_id.removeprefix(CHOICES_PREFIX))

        queueable = (
            media_id.startswith(QUEUE_ITEM_PREFIX)
            or media_source.is_media_source_id(media_id)
            or _is_playable_link(media_id)
            or (str(media_type).lower() in LIBRARY_ID_KEYS and media_id.isdigit())
        )
        if not queueable and mode in (EnqueueMode.NEXT, EnqueueMode.ADD):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="enqueue_not_supported",
                translation_placeholders={"media_type": str(media_type)},
            )
        # Checks that need no Kodi are done; now Kodi has to be awake.
        await self._async_wake_kodi()

        if media_id.startswith(QUEUE_ITEM_PREFIX):
            position = _queue_position(media_id)
            await self._run(self._data.queue.play_position(position))
            return

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            url = async_process_play_media_url(self.hass, play_item.url)
            item = await self._async_media_source_item(url)
            await self._run(self._data.queue.enqueue(item, mode))
            return

        if _is_playable_link(media_id) and (
            found := await backends.async_find_kodi_item(self._data.backend_context, media_id)
        ):
            # A media server link (for example Jellyfin's "Copy stream URL")
            await self._run(self._data.queue.enqueue(self._remember(found), mode))
            return

        if _is_playable_link(media_id):
            resolved = await self._async_resolve(media_id)
            if not resolved.queueable:
                if mode in (EnqueueMode.NEXT, EnqueueMode.ADD):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="play_only",
                        translation_placeholders={"resolver": resolved.resolver},
                    )
                # The add-on fills Kodi's video playlist itself and clears it first.
                mode = EnqueueMode.REPLACE
            self._data.queue.known.remember(resolved.file, resolved.title, resolved.thumbnail)
            await self._run(self._data.queue.enqueue(resolved.item, mode))
            return

        if (key := LIBRARY_ID_KEYS.get(str(media_type).lower())) and media_id.isdigit():
            await self._run(self._data.queue.enqueue({key: int(media_id)}, mode))
            return

        await self._forward(
            "play_media",
            media_content_type=media_type,
            media_content_id=media_id,
        )

    async def _async_announce(self, media_id: str, extra: dict[str, Any]) -> None:
        """Interrupt Kodi for a clip and continue afterwards (see docs/DESIGN.md)."""
        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            url = async_process_play_media_url(self.hass, play_item.url)
        elif _is_playable_link(media_id):
            url = media_id
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="announce_not_playable",
                translation_placeholders={"media_id": media_id},
            )
        volume = extra.get(ANNOUNCE_VOLUME, get_option(self._entry, CONF_ANNOUNCE_VOLUME))
        if volume is not None:
            try:
                volume = round(float(volume))
            except (TypeError, ValueError) as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="announce_volume",
                    translation_placeholders={"volume": str(volume)},
                ) from err
            if not 0 <= volume <= 100:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="announce_volume",
                    translation_placeholders={"volume": str(volume)},
                )

        source = self.hass.states.get(self._source_entity_id)
        was_off = source is not None and source.state == STATE_OFF
        await self._async_wake_kodi()
        if (
            was_off
            and get_option(self._entry, CONF_WAKE)
            and self._data.woken_for_announcement is None
        ):
            self._data.woken_for_announcement = time.monotonic()
        try:
            outcome = await self._data.announcer.announce(
                url,
                volume=volume,
                max_duration=get_option(self._entry, CONF_ANNOUNCE_MAX_DURATION),
                start_timeout=get_option(self._entry, CONF_ANNOUNCE_START_TIMEOUT),
            )
        except ClipError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="announce_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except AnnounceError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="announce_restore_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except KodiError as err:
            raise self._kodi_error(err) from err
        else:
            if outcome is Outcome.OVERRIDE:
                self._data.woken_for_announcement = None  # in use: don't switch off
        finally:
            self._schedule_queue_refresh()
            await self._async_power_off_after_announcement()

    async def _async_power_off_after_announcement(self) -> None:
        """Switch Kodi off again if it was woken for announcements and nobody uses it.

        Runs with Kodi to itself, like an announcement: a command sent now
        waits, and one sent since the wake keeps Kodi on. An announcement that
        arrives meanwhile switches Kodi off after itself instead.
        """
        announcer = self._data.announcer
        if self._data.woken_for_announcement is None or announcer.busy:
            return  # not woken for this, or the next announcement switches off
        async with announcer.exclusive():
            if (woken_at := self._data.woken_for_announcement) is None or announcer.queued:
                return
            uses = self._data.uses
            self._data.woken_for_announcement = None

            def unused() -> bool:
                return self._data.uses == uses and not announcer.queued

            try:
                if await self._async_idle_since(woken_at) and unused():
                    _LOGGER.debug("Switching Kodi off again after the announcement")
                    await self._async_switch_off(still_wanted=unused)
                else:
                    _LOGGER.debug("Kodi is in use; not switching it off after the announcement")
            except KodiError as err:
                _LOGGER.debug("Not switching Kodi off after the announcement: %s", err)
            except HomeAssistantError as err:
                _LOGGER.warning("Could not switch Kodi off after the announcement: %s", err)
            finally:
                if announcer.queued and self._data.uses == uses:
                    self._data.woken_for_announcement = woken_at  # the next one decides

    async def _async_idle_since(self, since: float) -> bool:
        """Return True if nothing plays and nobody used Kodi since then."""
        if await self._data.client.call("Player.GetActivePlayers"):
            return False
        seconds = int(time.monotonic() - since) - 1
        if seconds < 1:
            return True
        condition = f"System.IdleTime({seconds})"
        result = await self._data.client.call("XBMC.GetInfoBooleans", {"booleans": [condition]})
        return (result or {}).get(condition) is not False

    async def _async_media_source_item(self, url: str) -> dict[str, Any]:
        """Return the best Kodi item for a resolved media source URL (see backends)."""
        return self._remember(await backends.async_kodi_item(self._data.backend_context, url))

    def _remember(self, found: backends.BackendItem) -> dict[str, Any]:
        """Remember title and image of a backend's item for the queue; return the item."""
        if (file := found.item.get("file")) and (found.title or found.thumbnail):
            self._data.queue.known.remember(file, found.title, found.thumbnail)
        return found.item

    async def _async_wake_kodi(self) -> None:
        """Wake Kodi first if it is off (suspended), then wait until it answers.

        Better Kodi sends Wake on LAN packets itself, or "turn on" runs the
        user's Kodi turn-on automation. Kodi counts as awake once the Kodi
        entity is no longer "off". Calls that arrive during a wake wait for
        that same wake, so the automation runs once (a power toggle would
        otherwise switch off again).
        """
        # Kept per Kodi entity outside the config entry, so that a reload (for
        # example after changing options) doesn't start a second wake. As a
        # background task it is cancelled when Home Assistant stops.
        wakes: dict[str, asyncio.Task[None]] = self.hass.data.setdefault(WAKE_TASKS, {})
        running = wakes.get(self._source_entity_id)
        if running is None or running.done():
            source = self.hass.states.get(self._source_entity_id)
            if (
                not get_option(self._entry, CONF_WAKE)
                or source is None
                or source.state != STATE_OFF
            ):
                return
            running = wakes[self._source_entity_id] = self.hass.async_create_background_task(
                self._async_wake(), f"{DOMAIN} wake {self._source_entity_id}"
            )
        try:
            # shield: one caller giving up must not cancel the wake for the others
            await asyncio.shield(running)
        except asyncio.CancelledError:
            if not running.cancelled():
                raise  # this caller was cancelled
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="wake_cancelled"
            ) from None

    async def _async_wake(self) -> None:
        wake_on_lan = self._wake_on_lan
        if not wake_on_lan and not self.hass.bus.async_listeners().get(KODI_EVENT_TURN_ON):
            # The Kodi integration's "turn on" only fires this event; without an
            # automation listening, nothing would ever wake Kodi.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_turn_on_automation",
                translation_placeholders={"entity_id": self._source_entity_id},
            )
        timeout = get_option(self._entry, CONF_WAKE_TIMEOUT)
        awake = asyncio.Event()

        @callback
        def on_change(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            if new is not None and new.state not in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
                awake.set()

        unsubscribe = async_track_state_change_event(self.hass, [self._source_entity_id], on_change)
        try:
            _LOGGER.debug("%s is off; waking it before sending", self._source_entity_id)
            async with asyncio.timeout(timeout):
                if not wake_on_lan:
                    await self._call_source(SERVICE_TURN_ON)  # not _forward: no waiting here
                    await awake.wait()
                sent = 0
                while wake_on_lan and not awake.is_set():
                    # Packets get lost; Wake on LAN is safe to repeat (no toggle).
                    try:
                        await self._async_send_wake_on_lan()
                    except HomeAssistantError:
                        if not sent:
                            raise
                        _LOGGER.debug("Wake on LAN packet failed; trying again", exc_info=True)
                    sent += 1
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(awake.wait(), WAKE_ON_LAN_REPEAT)
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_on_lan_timeout" if wake_on_lan else "wake_timeout",
                translation_placeholders={
                    "entity_id": self._source_entity_id,
                    "seconds": str(timeout),
                },
            ) from err
        finally:
            unsubscribe()

        # The Kodi integration may report Kodi before its web server answers.
        # If it still doesn't, the next request reports the connection error.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(READY_TIMEOUT):
                while True:
                    try:
                        await self._data.client.ping()
                    except KodiError:
                        await asyncio.sleep(READY_POLL_INTERVAL)
                    else:
                        break
        await self._data.async_get_addons(force=True)

    async def async_clear_playlist(self) -> None:
        """Clear the video queue."""
        async with self._command():
            await self._run(self._data.queue.clear())

    async def _async_resolve(self, url: str) -> ResolvedMedia:
        addons = await self._data.async_get_addons()
        context = ResolveContext(
            session=self._data.web_session,
            addons=addons,
            online_lookups=bool(get_option(self._entry, CONF_ONLINE_LOOKUPS)),
        )
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
        except KodiError as err:
            raise self._kodi_error(err) from err
        self._schedule_queue_refresh()
        return result

    def _kodi_error(self, err: KodiError) -> HomeAssistantError:
        """Return the Home Assistant error for a Kodi error."""
        if isinstance(err, KodiRpcError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="kodi_error",
                translation_placeholders={"error": err.message},
            )
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": self._data.client.base_url},
        )

    # --- Services -------------------------------------------------------------

    async def async_call_method(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a Kodi JSON-RPC method and return its result."""
        async with self._command():
            return {"result": await self._run(self._data.client.call(method, params or None))}

    async def async_get_queue(self) -> dict[str, Any]:
        """Return the queue."""
        async with self._command(uses_kodi=False):
            items = await self._run(self._data.queue.items())
            status = await self._run(self._data.queue.status())
        return {
            "position": status.position,
            "length": len(items),
            "items": [item.as_dict() for item in items],
        }

    async def async_remove_from_queue(self, position: int) -> None:
        """Remove an item from the queue."""
        async with self._command():
            await self._run(self._data.queue.remove(position))

    async def async_move_queue_item(self, position: int, to_position: int) -> None:
        """Move an item within the queue."""
        async with self._command():
            await self._run(self._data.queue.move(position, to_position))

    async def async_handover(
        self, destination: str, keep_source_playing: bool = False, resume: bool = True
    ) -> None:
        """Continue the current playback on another Better Kodi entity."""
        async with self._command():
            await self._async_handover(destination, keep_source_playing, resume)

    async def _async_handover(
        self, destination: str, keep_source_playing: bool, resume: bool
    ) -> None:
        target = destination
        target_data = self._target_data(target)
        if (await self._run(self._data.queue.status())).player_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="nothing_playing",
            )
        # Wake the destination first, so the snapshot is taken when it can start.
        if isinstance(
            target_entity := self.hass.data[DATA_COMPONENT].get_entity(target),
            BetterKodiMediaPlayer,
        ):
            await target_entity._async_wake_kodi()
        snapshot: HandoverSnapshot = await self._run(self._data.queue.snapshot())
        try:
            resumed = await target_data.queue.restore(snapshot, resume=resume)
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
        if not resumed:
            _LOGGER.warning(
                "Handover from %s to %s: playback started at the start of the item",
                self.entity_id,
                target,
            )
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
        if media_content_id and media_content_id.startswith(CHOICES_PREFIX):
            return await self._async_browse_choices(media_content_id)

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
        if get_option(self._entry, CONF_BROWSE_CHOICES):
            result.children = [
                self._offer_choices(child) for child in result.children or []
            ] or None
        if media_content_type in (None, "library") and media_content_id in (None, "", "library"):
            result.children = [
                await self._async_browse_queue(as_child=True),
                *(result.children or []),
            ]
        return result

    def _offer_choices(self, item: BrowseMedia) -> BrowseMedia:
        """Let a playable item open "play now / next / add / replace".

        The item stays playable, so its play button still plays it right away.
        """
        if not item.can_play or item.can_expand or not _can_enqueue(item):
            return item
        choices_id = CHOICES_PREFIX + _encode(item.media_content_type, item.media_content_id)
        self._choice_items[choices_id] = item
        while len(self._choice_items) > 1000:
            del self._choice_items[next(iter(self._choice_items))]
        return BrowseMedia(
            media_class=item.media_class,
            media_content_id=choices_id,
            media_content_type=item.media_content_type,
            title=item.title,
            can_play=True,
            can_expand=True,
            thumbnail=item.thumbnail,
            children_media_class=item.media_class,
        )

    async def _async_browse_choices(self, choices_id: str) -> BrowseMedia:
        media_type, media_id = _decode(choices_id.removeprefix(CHOICES_PREFIX))
        item = self._choice_items.get(choices_id)
        texts = await self._async_browse_texts()
        children = [
            BrowseMedia(
                media_class=item.media_class if item else MediaClass.VIDEO,
                media_content_id=f"{ENQUEUE_PREFIX}{mode}/" + _encode(media_type, media_id),
                media_content_type=media_type,
                title=texts.get(mode, mode),
                can_play=True,
                can_expand=False,
                thumbnail=item.thumbnail if item else None,
            )
            for mode in CHOICE_ORDER
        ]
        return BrowseMedia(
            media_class=item.media_class if item else MediaClass.VIDEO,
            media_content_id=choices_id,
            media_content_type=media_type,
            title=item.title if item else texts.get("play", "play"),
            can_play=True,
            can_expand=True,
            thumbnail=item.thumbnail if item else None,
            children=children,
            children_media_class=children[0].media_class,
        )

    async def _async_browse_queue(self, *, as_child: bool = False) -> BrowseMedia:
        title = (await self._async_browse_texts()).get("queue", "Queue")
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
                # Kodi keeps the image of the playing entry on the player, not on
                # the playlist entry; the Kodi entity's picture shows it.
                thumbnail=(self.entity_picture if item.current else None)
                or self._queue_thumbnail(item.position, item.thumbnail),
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

    async def _async_browse_texts(self) -> dict[str, str]:
        """Return the translated media browser texts (queue folder, choices)."""
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.selector.browse.options."
        return {
            key.removeprefix(prefix): value
            for key, value in translations.items()
            if key.startswith(prefix)
        }

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
        return match.group(0).rstrip(TRAILING_PUNCTUATION)
    return text


def _encode(media_type: str, media_id: str) -> str:
    return f"{quote(str(media_type), safe='')}/{quote(media_id, safe='')}"


def _decode(encoded: str) -> tuple[str, str]:
    media_type, _, media_id = encoded.partition("/")
    return unquote(media_type), unquote(media_id)


def _can_enqueue(item: BrowseMedia) -> bool:
    """Return True for items that Better Kodi can put into the queue."""
    media_id = item.media_content_id or ""
    if media_source.is_media_source_id(media_id) or _is_playable_link(media_id):
        return True
    return str(item.media_content_type).lower() in LIBRARY_ID_KEYS and media_id.isdigit()


def _is_playable_link(media_id: str) -> bool:
    scheme, separator, _ = media_id.partition("://")
    return bool(separator) and scheme.lower() in PLAYABLE_SCHEMES


def _playing_item_changed(old: State | None, new: State | None) -> bool:
    """Return True if the Kodi entity moved to another item or state."""
    if old is None or new is None or old.state != new.state:
        return True
    return any(
        old.attributes.get(key) != new.attributes.get(key)
        for key in ("media_content_id", "media_title")
    )


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
