"""Prepare a fresh Home Assistant for the README screenshots.

Onboards a user, sets up the Kodi integration for the simulated Kodi, Better
Kodi on top of it, an area and a dashboard. Run Home Assistant first:

    hass -c <config dir>            (with this repository's custom_components linked in)
    python scripts/screenshots/setup_ha.py http://127.0.0.1:8123
"""

import asyncio
import json
import sys
from typing import Any

import aiohttp

USER, PASSWORD = "demo", "demo-password-1"
CLIENT_ID = "http://127.0.0.1:8123/"
KODI = {"host": "127.0.0.1", "port": 8080, "ws_port": 9090}


async def post(
    session: aiohttp.ClientSession, url: str, data: Any, token: str | None = None
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.post(url, json=data, headers=headers) as response:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"{url}: {response.status} {text}")
        return json.loads(text) if text else None


async def flow(
    session: aiohttp.ClientSession, base: str, token: str, handler: str, *steps: dict
) -> dict:
    result = await post(
        session, f"{base}/api/config/config_entries/flow", {"handler": handler}, token
    )
    for step in steps:
        if result["type"] != "form":
            break
        result = await post(
            session, f"{base}/api/config/config_entries/flow/{result['flow_id']}", step, token
        )
    if result["type"] != "create_entry":
        raise RuntimeError(f"{handler} flow ended with {result}")
    return result


class WebSocket:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws, self.id = ws, 0

    async def call(self, **message: Any) -> Any:
        self.id += 1
        await self.ws.send_json({"id": self.id, **message})
        while True:
            reply = await self.ws.receive_json()
            if reply.get("id") == self.id:
                if not reply.get("success", True):
                    raise RuntimeError(f"{message['type']}: {reply}")
                return reply.get("result")


async def main(base: str) -> None:
    async with aiohttp.ClientSession() as session:
        # Onboarding
        user = await post(
            session,
            f"{base}/api/onboarding/users",
            {
                "client_id": CLIENT_ID,
                "name": "Demo",
                "username": USER,
                "password": PASSWORD,
                "language": "en",
            },
        )
        async with session.post(
            f"{base}/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": user["auth_code"],
                "client_id": CLIENT_ID,
            },
        ) as response:
            token = (await response.json())["access_token"]
        await post(session, f"{base}/api/onboarding/core_config", {}, token)
        await post(session, f"{base}/api/onboarding/analytics", {}, token)
        await post(
            session,
            f"{base}/api/onboarding/integration",
            {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID},
            token,
        )

        # Kodi integration, then Better Kodi on top of it
        await flow(
            session,
            base,
            token,
            "kodi",
            {"host": KODI["host"], "port": KODI["port"], "ssl": False},
            {"ws_port": KODI["ws_port"]},
        )
        await asyncio.sleep(3)
        async with session.ws_connect(f"{base}/api/websocket") as raw:
            await raw.receive_json()
            await raw.send_json({"type": "auth", "access_token": token})
            await raw.receive_json()
            ws = WebSocket(raw)
            entities = await ws.call(type="config/entity_registry/list")
            source = next(
                e
                for e in entities
                if e["platform"] == "kodi" and e["entity_id"].startswith("media_player.")
            )
            await flow(
                session,
                base,
                token,
                "better_kodi",
                {"source_entity_id": source["entity_id"], "hide_source": True},
                {**KODI, "ssl": False},
            )
            await asyncio.sleep(3)
            areas = await ws.call(type="config/area_registry/list")
            area = next((a for a in areas if a["name"] == "Living Room"), None) or await ws.call(
                type="config/area_registry/create", name="Living Room"
            )
            await ws.call(
                type="config/device_registry/update",
                device_id=source["device_id"],
                area_id=area["area_id"],
                name_by_user="Living Room Kodi",
            )
            # Entity IDs as a user would have them (the Kodi entry is named after its host).
            for entity in await ws.call(type="config/entity_registry/list"):
                if (
                    entity["device_id"] == source["device_id"]
                    and "127_0_0_1" in entity["entity_id"]
                ):
                    await ws.call(
                        type="config/entity_registry/update",
                        entity_id=entity["entity_id"],
                        new_entity_id=entity["entity_id"].replace("127_0_0_1", "living_room_kodi"),
                    )
            entries = await ws.call(type="config_entries/get")
            for entry in entries:
                if entry["domain"] in ("kodi", "better_kodi"):
                    await ws.call(
                        type="config_entries/update",
                        entry_id=entry["entry_id"],
                        title="Living Room Kodi",
                    )
            await asyncio.sleep(2)
            entities = await ws.call(type="config/entity_registry/list")
            own = {e["entity_id"] for e in entities if e["platform"] == "better_kodi"}
            print(
                json.dumps(
                    {"token": token, "device_id": source["device_id"], "entities": sorted(own)}
                )
            )
            player = next(e for e in own if e.startswith("media_player."))
            link = next(e for e in own if e.startswith("text."))
            buttons = [
                (next(e for e in own if e.endswith(suffix)), name)
                for suffix, name in (
                    ("_play_link_now", "Play now"),
                    ("_play_link_next", "Play next"),
                    ("_add_link_to_queue", "Add to queue"),
                    ("_replace_queue_with_link", "Replace queue"),
                )
            ]
            await ws.call(
                type="lovelace/dashboards/create",
                url_path="kodi-demo",
                title="Kodi",
                icon="mdi:kodi",
                mode="storage",
                show_in_sidebar=True,
                require_admin=False,
            )
            await ws.call(
                type="lovelace/config/save",
                url_path="kodi-demo",
                config={
                    "views": [
                        {
                            "title": "Kodi",
                            "type": "sections",
                            "max_columns": 2,
                            "sections": [
                                {
                                    "type": "grid",
                                    "cards": [
                                        {"type": "media-control", "entity": player},
                                        {
                                            "type": "entities",
                                            "title": "Send a link to Kodi",
                                            "entities": [
                                                {"entity": link, "name": "Link"},
                                                {
                                                    "type": "buttons",
                                                    "entities": [
                                                        {"entity": b, "name": n} for b, n in buttons
                                                    ],
                                                },
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"))
