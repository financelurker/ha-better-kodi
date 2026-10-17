"""Tests for the Kodi JSON-RPC client."""

import base64
import json
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.better_kodi.kodi_client import (
    KodiAuthError,
    KodiClient,
    KodiConnectionError,
    KodiRpcError,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

URL = "http://10.0.0.5:8080/jsonrpc"


@pytest.fixture(autouse=True)
def patch_kodi_client() -> None:
    """Use the real client here (overrides the fake from conftest)."""
    return


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> KodiClient:
    return KodiClient(
        async_get_clientsession(hass),
        "10.0.0.5",
        8080,
        username="kodi",
        password="pw",
        ws_port=9090,
    )


async def test_call_returns_result_and_sends_auth(
    client: KodiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, json={"jsonrpc": "2.0", "id": 1, "result": "pong"})
    assert await client.call("JSONRPC.Ping") == "pong"
    _, _, payload, headers = aioclient_mock.mock_calls[0]
    assert payload == {"jsonrpc": "2.0", "id": 1, "method": "JSONRPC.Ping"}
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"kodi:pw").decode()


async def test_call_with_params(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, json={"jsonrpc": "2.0", "id": 1, "result": {"size": 3}})
    await client.call("Playlist.GetProperties", {"playlistid": 1})
    assert aioclient_mock.mock_calls[0][2]["params"] == {"playlistid": 1}


async def test_rpc_error(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        URL,
        json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params."}},
    )
    with pytest.raises(KodiRpcError) as err:
        await client.call("Playlist.Insert")
    assert err.value.code == -32602
    assert err.value.message == "Invalid params."


async def test_wrong_password(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, status=401)
    with pytest.raises(KodiAuthError):
        await client.ping()


@pytest.mark.parametrize("exc", [aiohttp.ClientError(), TimeoutError()])
async def test_unreachable(
    client: KodiClient, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    aioclient_mock.post(URL, exc=exc)
    with pytest.raises(KodiConnectionError):
        await client.ping()


async def test_server_error(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, status=500)
    with pytest.raises(KodiConnectionError):
        await client.ping()


async def test_image(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    image = "image://smb%3a%2f%2fnas%2fposter.jpg/"
    assert client.image_url(image) == (
        "http://10.0.0.5:8080/image/image%3A%2F%2Fsmb%253a%252f%252fnas%252fposter.jpg%2F"
    )
    aioclient_mock.get(
        client.image_url(image), content=b"jpeg", headers={"Content-Type": "image/jpeg"}
    )
    assert await client.fetch_image(image) == (b"jpeg", "image/jpeg")


async def test_image_missing(client: KodiClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(client.image_url("x"), status=404)
    assert await client.fetch_image("x") == (None, None)


async def test_urls(hass: HomeAssistant) -> None:
    session = async_get_clientsession(hass)
    assert (
        KodiClient(session, "kodi", 443, ssl=True, ws_port=9090).ws_url == "wss://kodi:9090/jsonrpc"
    )
    assert KodiClient(session, "kodi", 8080).ws_url is None


def test_notification_dispatch() -> None:
    received: list[tuple[str, dict]] = []

    def callback(method: str, data: dict) -> None:
        received.append((method, data))

    dispatch = KodiClient._dispatch
    dispatch(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "Playlist.OnAdd",
                "params": {"data": {"playlistid": 1}, "sender": "xbmc"},
            }
        ),
        callback,
    )
    dispatch(json.dumps({"jsonrpc": "2.0", "id": 4, "result": "OK"}), callback)
    dispatch("not json", callback)
    assert received == [("Playlist.OnAdd", {"playlistid": 1})]


async def test_notifications_without_ws_port(hass: HomeAssistant) -> None:
    client = KodiClient(async_get_clientsession(hass), "kodi", 8080)
    with patch.object(client.session, "ws_connect") as ws_connect:
        await client.run_notifications(lambda method, data: None)
    ws_connect.assert_not_called()
