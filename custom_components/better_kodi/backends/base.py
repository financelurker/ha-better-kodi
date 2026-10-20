"""The interface of a media-source backend.

A backend teaches Better Kodi about the items of one Home Assistant
integration that offers media in the media browser (for example Jellyfin).
Such an integration hands players a plain stream URL. Kodi then shows
"stream" as the title, and playback is not reported back to the media server.
A backend can do better:

- find the item in Kodi's own library, so Kodi knows title and artwork,
- or use the Kodi add-on of the media server, so watched state and resume work,
- and in any case tell Better Kodi the title and image for its queue.

See "Media-source backends" in the README for how to add one.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..kodi_client import KodiClient


@dataclass(slots=True, frozen=True)
class BackendContext:
    """What a backend may use."""

    # For requests to websites and media servers; checks certificates.
    session: aiohttp.ClientSession
    # The Kodi this item is meant for.
    client: KodiClient
    # Returns the IDs of the enabled video add-ons in Kodi (cached).
    get_addons: Callable[[], Awaitable[frozenset[str]]]


@dataclass(slots=True, frozen=True)
class BackendItem:
    """The Kodi playlist item for a media browser item."""

    # A Kodi playlist item: {"file": "…"} or a library item such as {"episodeid": 7}.
    item: dict[str, Any]
    # Title and image for Better Kodi's queue, if Kodi won't know them itself.
    title: str | None = None
    thumbnail: str | None = None


class MediaSourceBackend(ABC):
    """Maps the stream URLs of one media server to better Kodi items."""

    # Short name, used in logs.
    name: str

    @abstractmethod
    def recognizes(self, url: str) -> bool:
        """Return True if the URL belongs to this backend. Must not do I/O."""

    @abstractmethod
    async def async_kodi_item(self, context: BackendContext, url: str) -> BackendItem | None:
        """Return the Kodi item for a recognized URL, or None to queue the URL as is.

        Errors must not escape: return None instead (and log at debug level).
        """

    async def async_title(
        self, context: BackendContext, file: str
    ) -> tuple[str | None, str | None] | None:
        """Return title and image of a queue entry with a recognized URL.

        Used for entries that other apps put into the queue, or that were
        queued before Home Assistant restarted. None means unknown.
        """
        return None
