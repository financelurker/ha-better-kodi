"""Tests for the URL resolvers."""

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.better_kodi.resolvers import (
    ADDON_JELLYCON,
    ADDON_JELLYFIN,
    ADDON_SENDTOKODI,
    ADDON_YOUTUBE,
    RUMBLE_API,
    YOUTUBE_OEMBED,
    ResolveContext,
    ResolveError,
    async_lookup_title,
    async_resolve,
    parse_jellyfin_item_id,
    parse_youtube,
    pick_rumble_stream,
    strip_kodi_markup,
    youtube_from_kodi_path,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

VIDEO_ID = "dQw4w9WgXcQ"
JELLYFIN_ID = "0123456789abcdef0123456789abcdef"


def context(hass: HomeAssistant, *addons: str) -> ResolveContext:
    return ResolveContext(session=async_get_clientsession(hass), addons=frozenset(addons))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://www.youtube.com/watch?v={VIDEO_ID}", ("video", VIDEO_ID)),
        (f"https://youtube.com/watch?v={VIDEO_ID}&list=PL123&t=42", ("video", VIDEO_ID)),
        (f"https://m.youtube.com/watch?v={VIDEO_ID}", ("video", VIDEO_ID)),
        (f"https://music.youtube.com/watch?v={VIDEO_ID}", ("video", VIDEO_ID)),
        (f"https://youtu.be/{VIDEO_ID}?si=abc", ("video", VIDEO_ID)),
        (f"https://www.youtube.com/shorts/{VIDEO_ID}", ("video", VIDEO_ID)),
        (f"https://www.youtube.com/live/{VIDEO_ID}?feature=share", ("video", VIDEO_ID)),
        (f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}", ("video", VIDEO_ID)),
        (
            "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
            ("playlist", "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"),
        ),
        ("https://www.youtube.com/watch?v=tooshort", None),
        ("https://www.youtube.com/@somechannel", None),
        (f"https://example.com/watch?v={VIDEO_ID}", None),
    ],
)
def test_parse_youtube(url: str, expected: tuple[str, str] | None) -> None:
    assert parse_youtube(url) == expected


async def test_youtube_video_with_title(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        YOUTUBE_OEMBED,
        json={"title": "Never Gonna", "thumbnail_url": "https://i.ytimg.com/x.jpg"},
    )
    result = await async_resolve(f"https://youtu.be/{VIDEO_ID}", context(hass, ADDON_YOUTUBE))
    assert result.item == {"file": f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}"}
    assert (result.title, result.thumbnail, result.queueable) == (
        "Never Gonna",
        "https://i.ytimg.com/x.jpg",
        True,
    )


async def test_youtube_title_lookup_failure_is_harmless(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(YOUTUBE_OEMBED, status=404)
    result = await async_resolve(f"https://youtu.be/{VIDEO_ID}", context(hass, ADDON_YOUTUBE))
    assert result.resolver == "youtube"
    assert result.title is None


async def test_youtube_playlist_is_play_only(hass: HomeAssistant) -> None:
    result = await async_resolve(
        "https://www.youtube.com/playlist?list=PL123", context(hass, ADDON_YOUTUBE)
    )
    assert result.item == {
        "file": "plugin://plugin.video.youtube/play/?playlist_id=PL123&order=default&action=play"
    }
    assert not result.queueable


async def test_youtube_without_addon(hass: HomeAssistant) -> None:
    with pytest.raises(ResolveError, match="YouTube add-on"):
        await async_resolve(f"https://youtu.be/{VIDEO_ID}", context(hass))


async def test_youtube_without_addon_falls_back_to_sendtokodi(hass: HomeAssistant) -> None:
    url = f"https://youtu.be/{VIDEO_ID}"
    result = await async_resolve(url, context(hass, ADDON_SENDTOKODI))
    assert result.item == {"file": f"plugin://plugin.video.sendtokodi/?{url}"}


RUMBLE_VIDEO = {
    "title": "A Rumble video",
    "i": "https://sp.rmbl.ws/thumb.jpg",
    "live": 0,
    "ua": {
        "mp4": {
            "360": {"url": "https://rumble.cloud/v360.mp4", "meta": {"h": 360}},
            "1080": {"url": "https://rumble.cloud/v1080.mp4", "meta": {"h": 1080}},
            "720": {"url": "https://rumble.cloud/v720.mp4", "meta": {"h": 720}},
        },
        "hls": {"auto": {"url": "https://rumble.cloud/v.m3u8", "meta": {}}},
        "tar": {"720": {"url": "https://rumble.cloud/v.tar"}},
    },
}


async def test_rumble_page(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    page = "https://rumble.com/v6abcd-some-title.html"
    aioclient_mock.get(
        page,
        text='<script type="application/ld+json">{"embedUrl":"https://rumble.com/embed/v5xyz12/"}</script>',
    )
    aioclient_mock.get(RUMBLE_API, json=RUMBLE_VIDEO)
    result = await async_resolve(page, context(hass))
    assert result.item == {"file": "https://rumble.cloud/v1080.mp4"}
    assert result.title == "A Rumble video"
    assert result.resolver == "rumble"
    api_call = aioclient_mock.mock_calls[-1]
    assert dict(api_call[1].query) == {"request": "video", "ver": "2", "v": "v5xyz12"}


async def test_rumble_embed_link(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(RUMBLE_API, json=RUMBLE_VIDEO)
    result = await async_resolve("https://rumble.com/embed/v5xyz12/", context(hass))
    assert result.file == "https://rumble.cloud/v1080.mp4"


async def test_rumble_page_without_embed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    page = "https://rumble.com/v6abcd-some-title.html"
    aioclient_mock.get(page, text="<html>nothing here</html>")
    with pytest.raises(ResolveError, match="no embed ID"):
        await async_resolve(page, context(hass))


def test_rumble_live_prefers_hls() -> None:
    assert pick_rumble_stream({**RUMBLE_VIDEO, "live": 2}) == "https://rumble.cloud/v.m3u8"


def test_rumble_list_shaped_formats() -> None:
    video = {
        "ua": {
            "mp4": [
                {"url": "https://a/480.mp4", "meta": {"h": 480}},
                {"url": "https://a/720.mp4", "meta": {"h": 720}},
            ]
        }
    }
    assert pick_rumble_stream(video) == "https://a/720.mp4"


def test_rumble_without_streams() -> None:
    assert pick_rumble_stream({"ua": {"tar": {"720": {"url": "x"}}}}) is None


@pytest.mark.parametrize(
    "url",
    [
        f"https://jelly.example/web/#/details?id={JELLYFIN_ID}&serverId=abc",
        f"https://jelly.example/web/index.html#!/details?id={JELLYFIN_ID}",
        f"http://192.168.1.5:8096/web/#/details?id={JELLYFIN_ID.upper()}",
    ],
)
def test_parse_jellyfin(url: str) -> None:
    assert parse_jellyfin_item_id(url) == JELLYFIN_ID


def test_parse_jellyfin_rejects_other_pages() -> None:
    assert parse_jellyfin_item_id("https://jelly.example/web/#/home") is None
    assert parse_jellyfin_item_id("https://jelly.example/web/#/details?id=123") is None


async def test_jellyfin_for_kodi(hass: HomeAssistant) -> None:
    url = f"https://jelly.example/web/#/details?id={JELLYFIN_ID}"
    result = await async_resolve(url, context(hass, ADDON_JELLYFIN, ADDON_JELLYCON))
    assert result.item == {"file": f"plugin://plugin.video.jellyfin/?mode=play&id={JELLYFIN_ID}"}
    assert result.queueable


async def test_jellycon_is_play_only(hass: HomeAssistant) -> None:
    url = f"https://jelly.example/web/#/details?id={JELLYFIN_ID}"
    result = await async_resolve(url, context(hass, ADDON_JELLYCON))
    assert result.item == {
        "file": f"plugin://plugin.video.jellycon/?mode=PLAY&item_id={JELLYFIN_ID}"
    }
    assert not result.queueable


async def test_jellyfin_without_addon(hass: HomeAssistant) -> None:
    with pytest.raises(ResolveError, match="Jellyfin"):
        await async_resolve(f"https://j/web/#/details?id={JELLYFIN_ID}", context(hass))


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example/video.mp4",
        "https://cdn.example/live/stream.m3u8?token=1",
        "smb://nas/movies/film.mkv",
        "plugin://plugin.video.foo/?bar=1",
    ],
)
async def test_direct(hass: HomeAssistant, url: str) -> None:
    result = await async_resolve(url, context(hass, ADDON_SENDTOKODI))
    assert result.item == {"file": url}
    assert result.resolver == "direct"


async def test_other_pages_go_to_sendtokodi(hass: HomeAssistant) -> None:
    url = "https://vimeo.com/12345?x=1&y=2"
    result = await async_resolve(url, context(hass, ADDON_SENDTOKODI))
    assert result.item == {"file": f"plugin://plugin.video.sendtokodi/?{url}"}


async def test_other_pages_without_sendtokodi_pass_through(hass: HomeAssistant) -> None:
    url = "https://example.com/stream"
    result = await async_resolve(f"  {url} ", context(hass))
    assert result.item == {"file": url}
    assert result.resolver == "passthrough"


async def test_jellyfin_without_addon_does_not_use_sendtokodi(hass: HomeAssistant) -> None:
    with pytest.raises(ResolveError, match="Jellyfin"):
        await async_resolve(
            f"https://j/web/#/details?id={JELLYFIN_ID}", context(hass, ADDON_SENDTOKODI)
        )


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}", ("video", VIDEO_ID)),
        (
            f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}&incognito=false",
            ("video", VIDEO_ID),
        ),
        (
            f"plugin://plugin.video.youtube/?action=play_video&videoid={VIDEO_ID}",
            ("video", VIDEO_ID),
        ),
        (
            "plugin://plugin.video.youtube/play/?playlist_id=PL9&order=default&play=1",
            ("playlist", "PL9"),
        ),
        (
            "plugin://plugin.video.youtube/uri2addon/?uri=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D"
            + VIDEO_ID,
            ("video", VIDEO_ID),
        ),
        (f"plugin://plugin.video.sendtokodi/?https://youtu.be/{VIDEO_ID}", ("video", VIDEO_ID)),
        (f"https://www.youtube.com/watch?v={VIDEO_ID}", ("video", VIDEO_ID)),
        ("plugin://plugin.video.jellyfin/?mode=play&id=1", None),
        ("smb://nas/film.mkv", None),
    ],
)
def test_youtube_from_kodi_path(file: str, expected: tuple[str, str] | None) -> None:
    assert youtube_from_kodi_path(file) == expected


async def test_lookup_title(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(YOUTUBE_OEMBED, json={"title": "Queued by Cast Kodi", "thumbnail_url": "t"})
    session = async_get_clientsession(hass)
    file = f"plugin://plugin.video.youtube/play/?video_id={VIDEO_ID}&incognito=false"
    assert await async_lookup_title(session, file) == ("Queued by Cast Kodi", "t")
    assert dict(aioclient_mock.mock_calls[0][1].query)["url"] == (
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )
    assert await async_lookup_title(session, "smb://nas/film.mkv") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("[B]Bold[/B] and [COLOR red]red[/COLOR]", "Bold and red"), ("Line[CR]break", "Line break")],
)
def test_strip_kodi_markup(text: str, expected: str) -> None:
    assert strip_kodi_markup(text) == expected
