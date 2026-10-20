"""Media-source backends: better Kodi items for media browser items.

This is an extension point. To support another media server, add a module with
a subclass of ``MediaSourceBackend`` (see ``base.py``) and list an instance in
``BACKENDS``. The first backend that recognizes a URL handles it.
"""

import logging

from .base import BackendContext, BackendItem, MediaSourceBackend
from .jellyfin import JellyfinBackend

_LOGGER = logging.getLogger(__name__)

BACKENDS: tuple[MediaSourceBackend, ...] = (JellyfinBackend(),)

__all__ = [
    "BACKENDS",
    "BackendContext",
    "BackendItem",
    "MediaSourceBackend",
    "async_kodi_item",
    "async_title",
]


def _backend_for(url: str) -> MediaSourceBackend | None:
    for backend in BACKENDS:
        try:
            if backend.recognizes(url):
                return backend
        except Exception:
            _LOGGER.exception("Media-source backend %s failed on %s", backend.name, url)
    return None


async def async_kodi_item(context: BackendContext, url: str) -> BackendItem:
    """Return the Kodi item for a resolved media browser URL."""
    if (backend := _backend_for(url)) is not None:
        try:
            if (found := await backend.async_kodi_item(context, url)) is not None:
                return found
        except Exception:
            _LOGGER.exception("Media-source backend %s failed on %s", backend.name, url)
    return BackendItem(item={"file": url})


async def async_title(context: BackendContext, file: str) -> tuple[str | None, str | None] | None:
    """Return title and image of a queue entry, if a backend knows them."""
    if (backend := _backend_for(file)) is None:
        return None
    try:
        return await backend.async_title(context, file)
    except Exception:
        _LOGGER.exception("Media-source backend %s failed on %s", backend.name, file)
        return None
