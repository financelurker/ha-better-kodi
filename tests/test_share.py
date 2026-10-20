"""Tests for links shared from the Home Assistant app on a phone."""

import time
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_kodi.const import (
    CONF_SHARE_DEVICES,
    CONF_SHARE_ENQUEUE,
    CONF_SHARE_NOTIFY,
    DEFAULT_OPTIONS,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

from .conftest import KodiSource, setup_better_kodi
from .fake_kodi import FakeKodi

FILE = "https://cdn.example/a.mp4"


def add_phone(
    hass: HomeAssistant, name: str, app_id: str, *, notify: bool = True
) -> tuple[str, list[ServiceCall]]:
    """A phone with the Home Assistant app and its notify service."""
    entry = MockConfigEntry(domain="mobile_app", data={"device_name": name}, title=name)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("mobile_app", app_id)}, name=name
    )
    notifications: list[ServiceCall] = []
    if notify:
        hass.services.async_register("notify", "mobile_app_" + slugify(name), notifications.append)
    return device.id, notifications


async def share(hass: HomeAssistant, device_id: str | None = None, **data: Any) -> None:
    hass.bus.async_fire("mobile_app.share", {"device_id": device_id, **data})
    await hass.async_block_till_done(wait_background_tasks=True)


def set_options(hass: HomeAssistant, entry: ConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(entry, options={**DEFAULT_OPTIONS, **options})


@pytest.fixture
async def one_kodi(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> FakeKodi:
    await setup_better_kodi(hass, kodi_source)
    return fakes[kodi_source.host]


async def test_shared_link_is_queued(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url=FILE, subject="A video")
    assert one_kodi.files() == [FILE]
    assert notifications == []


async def test_link_inside_shared_text(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    """Brave and others share text with the link in it."""
    phone, _ = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, text=f"Look at this: {FILE}.")
    assert one_kodi.files() == [FILE]


async def test_device_id_of_the_app_is_understood(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    add_phone(hass, "Pixel 8", "app-1")
    await share(hass, "app-1", url=FILE)
    assert one_kodi.files() == [FILE]


async def test_shares_without_link_are_ignored(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, text="just some words")
    await share(hass, phone, url="smb://nas/film.mkv")
    assert one_kodi.files() == []
    assert notifications == []


async def test_unknown_phone_with_one_kodi(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    await share(hass, None, url=FILE)
    assert one_kodi.files() == [FILE]


async def test_error_is_reported_on_the_phone(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    hass.config.language = "de"
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url="https://youtu.be/dQw4w9WgXcQ")  # no YouTube add-on
    assert one_kodi.files() == []
    (note,) = notifications
    assert note.data["message"].startswith("Der geteilte Link konnte auf Wohnzimmer Kodi nicht")
    assert "YouTube" in note.data["message"]


@pytest.fixture
async def two_kodis(
    hass: HomeAssistant,
    kodi_source: KodiSource,
    make_kodi_source,
    fakes: dict[str, FakeKodi],
) -> tuple[ConfigEntry, FakeKodi, ConfigEntry, FakeKodi]:
    living = await setup_better_kodi(hass, kodi_source)
    office_source = await make_kodi_source("Büro Kodi", "10.0.0.6")
    office = await setup_better_kodi(hass, office_source)
    hass.config_entries.async_update_entry(office, title="Büro Kodi")
    return living, fakes[kodi_source.host], office, fakes[office_source.host]


async def test_phone_is_asked_which_kodi(hass: HomeAssistant, two_kodis) -> None:
    _, living_kodi, _, office_kodi = two_kodis
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url=FILE, subject="Ein Video")
    assert living_kodi.files() == office_kodi.files() == []
    (note,) = notifications
    actions = note.data["data"]["actions"]
    assert sorted(action["title"] for action in actions) == ["Büro Kodi", "Wohnzimmer Kodi"]
    assert "Ein Video" in note.data["message"]

    (office_action,) = [a for a in actions if a["title"] == "Büro Kodi"]
    hass.bus.async_fire(
        "mobile_app_notification_action", {"action": office_action["action"], "device_id": "app-1"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert office_kodi.files() == [FILE]
    assert living_kodi.files() == []

    # A second tap (or an old notification) does nothing.
    hass.bus.async_fire(
        "mobile_app_notification_action", {"action": office_action["action"], "device_id": "app-1"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert office_kodi.files() == [FILE]


async def test_playing_kodi_is_offered_first(
    hass: HomeAssistant, two_kodis, make_kodi_source
) -> None:
    living, _, office, _ = two_kodis
    hass.states.async_set(living.runtime_data.source_entity_id, "idle")
    hass.states.async_set(office.runtime_data.source_entity_id, "playing")
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url=FILE)
    assert notifications[0].data["data"]["actions"][0]["title"] == "Büro Kodi"


async def test_phone_assigned_to_one_kodi(hass: HomeAssistant, two_kodis) -> None:
    _, living_kodi, office, office_kodi = two_kodis
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    other, _ = add_phone(hass, "Tablet", "app-2")
    set_options(hass, office, **{CONF_SHARE_DEVICES: [phone]})
    await hass.async_block_till_done(wait_background_tasks=True)
    await share(hass, phone, url=FILE)
    assert office_kodi.files() == [FILE]
    assert living_kodi.files() == []
    assert notifications == []
    # The tablet isn't assigned anywhere: it uses the Kodis open to all phones.
    await share(hass, other, url=FILE)
    assert living_kodi.files() == [FILE]


async def test_sharing_switched_off_on_one_kodi(hass: HomeAssistant, two_kodis) -> None:
    living, living_kodi, _, office_kodi = two_kodis
    set_options(hass, living, **{CONF_SHARE_ENQUEUE: "off"})
    await hass.async_block_till_done(wait_background_tasks=True)
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url=FILE)
    assert office_kodi.files() == [FILE]
    assert living_kodi.files() == []
    assert notifications == []


async def test_several_kodis_and_unknown_phone(hass: HomeAssistant, two_kodis) -> None:
    """Nobody can be asked: nothing is played on a Kodi chosen at random."""
    _, living_kodi, _, office_kodi = two_kodis
    await share(hass, None, url=FILE)
    assert living_kodi.files() == office_kodi.files() == []


async def tap(hass: HomeAssistant, action: str, device_id: str | None) -> None:
    hass.bus.async_fire(
        "mobile_app_notification_action", {"action": action, "device_id": device_id}
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_only_the_asked_phone_may_answer(hass: HomeAssistant, two_kodis) -> None:
    _, living_kodi, _, office_kodi = two_kodis
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    add_phone(hass, "Tablet", "app-2")
    await share(hass, phone, url=FILE)
    action = notifications[0].data["data"]["actions"][0]["action"]
    await tap(hass, action, "app-2")  # another phone
    await tap(hass, action.rsplit("_", 1)[0] + "_WRONGENTRY", "app-1")  # forged entry
    assert living_kodi.files() == office_kodi.files() == []
    await tap(hass, action, "app-1")  # the real answer still works
    assert len(living_kodi.files() + office_kodi.files()) == 1


async def test_old_question_expires(hass: HomeAssistant, two_kodis) -> None:
    _, living_kodi, _, office_kodi = two_kodis
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url=FILE)
    action = notifications[0].data["data"]["actions"][0]["action"]
    later = time.monotonic() + 601
    with patch("custom_components.better_kodi.share.time.monotonic", return_value=later):
        await tap(hass, action, "app-1")
    assert living_kodi.files() == office_kodi.files() == []


async def test_no_notifications_falls_back_to_the_playing_kodi(
    hass: HomeAssistant, two_kodis
) -> None:
    living, living_kodi, office, office_kodi = two_kodis
    hass.states.async_set(living.runtime_data.source_entity_id, "idle")
    hass.states.async_set(office.runtime_data.source_entity_id, "playing")
    phone, _ = add_phone(hass, "Pixel 8", "app-1", notify=False)
    await share(hass, phone, url=FILE)
    assert office_kodi.files() == [FILE]
    assert living_kodi.files() == []


async def test_error_notifications_can_be_switched_off(
    hass: HomeAssistant, kodi_source: KodiSource, fakes: dict[str, FakeKodi]
) -> None:
    entry = await setup_better_kodi(hass, kodi_source)
    set_options(hass, entry, **{CONF_SHARE_NOTIFY: False})
    await hass.async_block_till_done()
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    await share(hass, phone, url="https://youtu.be/dQw4w9WgXcQ")
    assert notifications == []


async def test_unexpected_error_is_still_reported(hass: HomeAssistant, one_kodi: FakeKodi) -> None:
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    with patch(
        "custom_components.better_kodi.media_player.BetterKodiMediaPlayer.async_play_media",
        side_effect=RuntimeError("boom"),
    ):
        await share(hass, phone, url=FILE)
    assert "boom" in notifications[0].data["message"]


async def test_phone_listed_on_two_kodis_is_asked_between_them(
    hass: HomeAssistant, two_kodis, make_kodi_source
) -> None:
    living, _, office, _ = two_kodis
    third = await setup_better_kodi(hass, await make_kodi_source("Keller Kodi", "10.0.0.7"))
    hass.config_entries.async_update_entry(third, title="Keller Kodi")
    phone, notifications = add_phone(hass, "Pixel 8", "app-1")
    set_options(hass, living, **{CONF_SHARE_DEVICES: [phone]})
    set_options(hass, office, **{CONF_SHARE_DEVICES: [phone]})
    await hass.async_block_till_done()
    await share(hass, phone, url=FILE)
    titles = sorted(a["title"] for a in notifications[0].data["data"]["actions"])
    assert titles == ["Büro Kodi", "Wohnzimmer Kodi"]
    assert third.title not in titles
