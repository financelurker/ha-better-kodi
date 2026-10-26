"""Minimal Kodi JSON-RPC client used by Better Kodi.

Requests go over HTTP. Notifications (queue changes, playback events) are
received over Kodi's WebSocket interface, which has no authentication.
"""

import asyncio
import base64
from collections.abc import Callable
import itertools
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
RECONNECT_DELAYS = (5, 10, 30, 60)

type NotificationCallback = Callable[[str, dict[str, Any]], None]


class KodiError(Exception):
    """Base error for Kodi communication."""


class KodiConnectionError(KodiError):
    """Kodi could not be reached."""


class KodiAuthError(KodiError):
    """Kodi rejected the credentials."""


class KodiRpcError(KodiError):
    """Kodi answered a request with an error."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        """Store the JSON-RPC error details."""
        super().__init__(f"{method} failed: {message} ({code})")
        self.method = method
        self.code = code
        self.message = message


class KodiClient:
    """Talk to one Kodi instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        ssl: bool = False,
        ws_port: int | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._host = host
        self._port = port
        self._ssl = ssl
        self._ws_port = ws_port
        self._headers: dict[str, str] = {}
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"
        self._ids = itertools.count(1)
        self.notifications_connected = False

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the HTTP session, for other requests made on Kodi's behalf."""
        return self._session

    @property
    def base_url(self) -> str:
        """Return the HTTP base URL of Kodi."""
        scheme = "https" if self._ssl else "http"
        return f"{scheme}://{self._host}:{self._port}"

    @property
    def ws_url(self) -> str | None:
        """Return the WebSocket URL, or None if WebSocket is not configured."""
        if not self._ws_port:
            return None
        scheme = "wss" if self._ssl else "ws"
        return f"{scheme}://{self._host}:{self._ws_port}/jsonrpc"

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a JSON-RPC method and return its result."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
        }
        if params:
            payload["params"] = params
        try:
            async with self._session.post(
                f"{self.base_url}/jsonrpc",
                json=payload,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise KodiAuthError("Kodi rejected the user name or password")
                response.raise_for_status()
                data = await response.json(content_type=None)
        except KodiAuthError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise KodiConnectionError(f"{method}: {err}") from err

        if not isinstance(data, dict):
            raise KodiConnectionError(f"{method}: unexpected response {data!r}")
        if (error := data.get("error")) is not None:
            raise KodiRpcError(
                method,
                error.get("code") if isinstance(error, dict) else None,
                error.get("message", str(error)) if isinstance(error, dict) else str(error),
            )
        return data.get("result")

    async def ping(self) -> None:
        """Check that Kodi answers."""
        await self.call("JSONRPC.Ping")

    def image_url(self, kodi_image: str) -> str:
        """Return the HTTP URL for a Kodi image path (thumbnail, art)."""
        return f"{self.base_url}/image/{quote(kodi_image, safe='')}"

    async def fetch_image(self, kodi_image: str) -> tuple[bytes | None, str | None]:
        """Download an image from Kodi's web server."""
        try:
            async with self._session.get(
                self.image_url(kodi_image), headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    return None, None
                return await response.read(), response.headers.get("Content-Type")
        except aiohttp.ClientError, TimeoutError:
            return None, None

    async def run_notifications(self, callback: NotificationCallback) -> None:
        """Receive notifications over WebSocket until cancelled.

        Reconnects with increasing delays when the connection drops.
        """
        if (url := self.ws_url) is None:
            return
        attempt = 0
        while True:
            try:
                async with self._session.ws_connect(url, heartbeat=30) as ws:
                    attempt = 0
                    _LOGGER.debug("Connected to %s", url)
                    self.notifications_connected = True
                    async for message in ws:
                        if message.type is aiohttp.WSMsgType.TEXT:
                            self._dispatch(message.data, callback)
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("WebSocket %s unavailable: %s", url, err)
            finally:
                self.notifications_connected = False
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    @staticmethod
    def _dispatch(raw: str, callback: NotificationCallback) -> None:
        """Pass a notification message on to the callback."""
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict) or "method" not in message:
            return
        params = message.get("params") or {}
        data = params.get("data") if isinstance(params, dict) else None
        callback(message["method"], data if isinstance(data, dict) else {})
