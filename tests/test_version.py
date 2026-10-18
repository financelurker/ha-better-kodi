"""The version Home Assistant shows is the one in manifest.json, and nothing else."""

import json
from pathlib import Path

from custom_components.better_kodi.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

MANIFEST = Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json"


async def test_shown_version_is_the_manifest_version(hass: HomeAssistant) -> None:
    manifest_version = json.loads(MANIFEST.read_text())["version"]
    integration = await async_get_integration(hass, DOMAIN)
    # Placeholder in the repository; the release workflow writes the tag here.
    assert str(integration.version) == manifest_version == "0.0.0"
