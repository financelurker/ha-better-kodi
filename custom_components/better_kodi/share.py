"""Links shared from the Home Assistant app ("Share" → Home Assistant on the phone).

The Android app fires ``mobile_app.share`` with ``url``, ``text``, ``subject``
and ``device_id``. Better Kodi picks the Kodi for it:

- Every Better Kodi entry with sharing on is a candidate, unless it is limited
  to certain phones; entries that name the sending phone win over the others.
- One candidate: the link goes there.
- Several: the phone gets a notification with one button per Kodi
  (``mobile_app_notification_action``), and the link goes where you tap.

Errors are sent back to the phone, because that's where you are.
"""

from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ENQUEUE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import slugify

from .const import CONF_SHARE_DEVICES, CONF_SHARE_ENQUEUE, CONF_SHARE_NOTIFY, DOMAIN, SHARE_OFF

_LOGGER = logging.getLogger(__name__)

SHARE_EVENT = "mobile_app.share"
ACTION_EVENT = "mobile_app_notification_action"
ACTION_PREFIX = "BETTER_KODI_SHARE_"
MOBILE_APP = "mobile_app"
MAX_BUTTONS = 3  # Android shows at most three buttons on a notification
PENDING_SECONDS = 600
SHARE_FIELDS = ("url", "text", "subject")
STATE_RANK = {"playing": 0, "paused": 1, "buffering": 1, "on": 2, "idle": 2}


@dataclass(slots=True)
class Phone:
    """The phone a link came from."""

    device_id: str | None
    notify_service: str | None  # notify.<service>, if the phone has one


@dataclass(slots=True)
class PendingShare:
    """A link waiting for the user to pick a Kodi on the phone."""

    link: str
    phone: Phone
    entry_ids: tuple[str, ...]
    created: float
    context: Context


@callback
def async_setup_share(hass: HomeAssistant) -> None:
    """Listen for shared links and for the buttons of "which Kodi?" notifications."""
    shares = Shares(hass)
    hass.bus.async_listen(SHARE_EVENT, shares.on_share)
    hass.bus.async_listen(ACTION_EVENT, shares.on_action)


def _sender(event: Event) -> Any:
    """Return the app's ID of the sending phone (Android: device_id, iOS: another key)."""
    return event.data.get("device_id") or event.data.get("sourceDevicePermanentID")


def link_from_share(data: dict[str, Any]) -> str | None:
    """Return the first http(s) link in a share event, else None."""
    # Imported here: media_player imports this package's __init__.
    from .media_player import extract_link

    for key in SHARE_FIELDS:
        if isinstance(value := data.get(key), str) and value.strip():
            link = extract_link(value)
            if link.lower().startswith(("http://", "https://")):
                return link
    return None


class Shares:
    """Routes shared links to Kodi."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self.pending: dict[str, PendingShare] = {}

    @callback
    def on_share(self, event: Event) -> None:
        """Handle a shared link."""
        if (link := link_from_share(event.data)) is None:
            return  # not a link; maybe another automation wants it
        phone = self._phone(_sender(event))
        if not (candidates := self._candidates(phone)):
            _LOGGER.debug("Shared link %s: no Better Kodi takes links from this phone", link)
            return
        context = Context(parent_id=event.context.id, user_id=event.context.user_id)
        if len(candidates) == 1:
            self.hass.async_create_background_task(
                self._async_send(candidates[0], link, phone, context), f"{DOMAIN} share"
            )
            return
        self.hass.async_create_background_task(
            self._async_ask(candidates, link, event.data.get("subject"), phone, context),
            f"{DOMAIN} share choice",
        )

    @callback
    def on_action(self, event: Event) -> None:
        """Handle a button of a "which Kodi?" notification."""
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
            return
        token, _, entry_id = action.removeprefix(ACTION_PREFIX).partition("_")
        self._forget_old()
        pending = self.pending.get(token)
        if pending is None or entry_id not in pending.entry_ids:
            _LOGGER.debug("Ignoring an old or unknown share choice %s", action)
            return
        # Only the phone that was asked may answer.
        if self._phone(_sender(event)).device_id != pending.phone.device_id:
            _LOGGER.debug("Ignoring a share choice from another device")
            return
        del self.pending[token]
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.state is not ConfigEntryState.LOADED
            or entry.options.get(CONF_SHARE_ENQUEUE, "add") == SHARE_OFF
        ):
            return
        self.hass.async_create_background_task(
            self._async_send(entry, pending.link, pending.phone, pending.context),
            f"{DOMAIN} share",
        )

    # --- Choosing ------------------------------------------------------------

    def _phone(self, device_id: Any) -> Phone:
        if not isinstance(device_id, str) or not device_id:
            return Phone(None, None)
        registry = dr.async_get(self.hass)
        # The app sends its registration ID, which is the device's identifier.
        device = registry.async_get_device(identifiers={(MOBILE_APP, device_id)}) or (
            registry.async_get(device_id)
        )
        entry = (
            self.hass.config_entries.async_get_entry(device.config_entry_id)
            if device is not None and device.config_entry_id
            else None
        )
        if device is None or entry is None or entry.domain != MOBILE_APP:
            return Phone(None, None)
        service = None
        if name := entry.data.get("device_name"):
            candidate = f"{MOBILE_APP}_{slugify(name)}"
            if self.hass.services.has_service("notify", candidate):
                service = candidate
        return Phone(device.id, service)

    def _candidates(self, phone: Phone) -> list[ConfigEntry]:
        """Return the Kodis that take this phone's links, the playing ones first."""
        active = [
            entry
            for entry in self.hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.options.get(CONF_SHARE_ENQUEUE, "add") != SHARE_OFF
        ]
        named = [e for e in active if phone.device_id in e.options.get(CONF_SHARE_DEVICES, [])]
        anyone = [e for e in active if not e.options.get(CONF_SHARE_DEVICES)]
        candidates = named or anyone
        return sorted(candidates, key=self._rank)

    def _rank(self, entry: ConfigEntry) -> tuple[int, str]:
        state = self.hass.states.get(entry.runtime_data.source_entity_id)
        return STATE_RANK.get(state.state if state else "", 3), entry.title

    # --- Sending ---------------------------------------------------------------

    async def _async_send(
        self, entry: ConfigEntry, link: str, phone: Phone, context: Context
    ) -> None:
        player = er.async_get(self.hass).async_get_entity_id(
            MEDIA_PLAYER_DOMAIN, DOMAIN, entry.entry_id
        )
        if player is None:
            return
        try:
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_PLAY_MEDIA,
                {
                    ATTR_ENTITY_ID: player,
                    ATTR_MEDIA_CONTENT_TYPE: "url",
                    ATTR_MEDIA_CONTENT_ID: link,
                    ATTR_MEDIA_ENQUEUE: entry.options.get(CONF_SHARE_ENQUEUE, "add"),
                },
                blocking=True,
                context=context,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Shared link %s could not be sent to %s: %s", link, entry.title, err)
            error = await self._message(err)
        except Exception as err:  # the phone already said "shared"; it must hear about it
            _LOGGER.exception("Shared link %s could not be sent to %s", link, entry.title)
            error = str(err) or type(err).__name__
        else:
            return
        if entry.options.get(CONF_SHARE_NOTIFY, True):
            texts = await self._texts()
            await self._async_notify(
                phone,
                texts.get("share_failed", "{kodi}: {error}").format(kodi=entry.title, error=error),
            )

    async def _async_ask(
        self,
        candidates: list[ConfigEntry],
        link: str,
        subject: Any,
        phone: Phone,
        context: Context,
    ) -> None:
        texts = await self._texts()
        title = subject.strip() if isinstance(subject, str) and subject.strip() else link
        if phone.notify_service is None:
            # No way to ask on the phone (unknown phone, or no notifications).
            # Unambiguous after all if exactly one of them is playing.
            playing = [e for e in candidates if self._rank(e)[0] == 0]
            if len(playing) == 1:
                await self._async_send(playing[0], link, phone, context)
                return
            _LOGGER.warning(
                "Shared link %s: several Kodis could take it (%s), but the phone can't be "
                "asked. Limit sharing to certain phones in Better Kodi's options",
                link,
                ", ".join(entry.title for entry in candidates),
            )
            return
        self._forget_old()
        choices = candidates[:MAX_BUTTONS]
        if len(candidates) > MAX_BUTTONS:
            _LOGGER.debug("Shared link %s: only the first %s Kodis are offered", link, MAX_BUTTONS)
        token = secrets.token_hex(8)
        self.pending[token] = PendingShare(
            link, phone, tuple(entry.entry_id for entry in choices), time.monotonic(), context
        )
        await self._async_notify(
            phone,
            texts.get("share_choose", "{title}").format(title=title),
            actions=[
                {"action": f"{ACTION_PREFIX}{token}_{entry.entry_id}", "title": entry.title}
                for entry in choices
            ],
            tag=f"better_kodi_share_{token}",
        )

    async def _async_notify(
        self,
        phone: Phone,
        message: str,
        *,
        actions: list[dict[str, str]] | None = None,
        tag: str | None = None,
    ) -> None:
        if phone.notify_service is None:
            return
        data: dict[str, Any] = {}
        if actions:
            data["actions"] = actions
        if tag:
            data["tag"] = tag
        try:
            await self.hass.services.async_call(
                "notify",
                phone.notify_service,
                {"title": "Better Kodi", "message": message, **({"data": data} if data else {})},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not notify %s: %s", phone.notify_service, err)

    def _forget_old(self) -> None:
        limit = time.monotonic() - PENDING_SECONDS
        for token in [t for t, share in self.pending.items() if share.created < limit]:
            del self.pending[token]

    async def _texts(self) -> dict[str, str]:
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "exceptions", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.exceptions."
        return {
            key.removeprefix(prefix).removesuffix(".message"): value
            for key, value in translations.items()
            if key.startswith(prefix)
        }

    async def _message(self, err: HomeAssistantError) -> str:
        """Return the error in the user's language, if it is one of Better Kodi's."""
        if err.translation_domain == DOMAIN and err.translation_key:
            template = (await self._texts()).get(err.translation_key)
            if template:
                try:
                    return template.format(**(err.translation_placeholders or {}))
                except KeyError:
                    pass
        return str(err)
