"""Tests for announcements: interrupt Kodi, play a clip, continue where it was."""

import asyncio
from typing import Any

import pytest

from custom_components.better_kodi import announce
from custom_components.better_kodi.announce import (
    Announcer,
    ClipError,
    Outcome,
    RestoreError,
    Stream,
)

from .fake_kodi import FakeKodi

CLIP = "http://ha.local:8123/api/tts_proxy/abc.mp3"
CLIP_2 = "http://ha.local:8123/api/tts_proxy/def.mp3"
MOVIE = "smb://nas/movies/Winter Orchard.mkv"
AT_42_17 = {"hours": 0, "minutes": 42, "seconds": 17, "milliseconds": 0}

AUDIO = [
    {"index": 0, "language": "eng", "name": "English 5.1"},
    {"index": 1, "language": "ger", "name": "Deutsch 5.1"},
]
SUBTITLES = [
    {"index": 0, "language": "eng", "name": ""},
    {"index": 1, "language": "ger", "name": "Forced"},
]


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announce, "POLL_INTERVAL", 0.05)


@pytest.fixture(params=["websocket", "polling"])
def kodi(request: pytest.FixtureRequest) -> FakeKodi:
    """A Kodi playing the second entry of the queue at 0:42:17 (German audio, subtitles)."""
    kodi = FakeKodi()
    kodi.playlist = [
        {"file": "smb://nas/a.mkv", "label": "a", "type": "unknown"},
        {"file": MOVIE, "label": "Winter Orchard", "type": "unknown"},
    ]
    kodi.player_active, kodi.position = True, 1
    kodi.time = dict(AT_42_17)
    kodi.audio_streams, kodi.audio_index = AUDIO, 1
    kodi.subtitles, kodi.subtitle_index, kodi.subtitle_enabled = SUBTITLES, 1, True
    kodi.websocket = request.param == "websocket"
    return kodi


@pytest.fixture
def announcer(kodi: FakeKodi) -> Announcer:
    announcer = Announcer(kodi)  # FakeKodi answers like KodiClient
    if kodi.websocket:
        kodi.notify = announcer.on_notification
    return announcer


def opened(kodi: FakeKodi) -> list[dict[str, Any]]:
    return [params for method, params in kodi.calls if method == "Player.Open"]


async def test_interrupts_and_continues_the_queue(kodi: FakeKodi, announcer: Announcer) -> None:
    files_before = kodi.files()
    assert await announcer.announce(CLIP) is Outcome.DONE
    assert kodi.clips == [CLIP]
    assert opened(kodi)[-1] == {"item": {"playlistid": 1, "position": 1}}
    assert kodi.player_active
    assert kodi.position == 1
    assert kodi.time == AT_42_17
    assert kodi.files() == files_before  # the queue is untouched
    assert kodi.speed == 1
    assert not announcer.busy


async def test_continues_paused_with_the_same_tracks(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.speed = 0
    kodi.forget_streams = True  # a stream URL: Kodi doesn't remember the tracks itself
    await announcer.announce(CLIP)
    assert kodi.speed == 0
    assert (kodi.audio_index, kodi.subtitle_index, kodi.subtitle_enabled) == (1, 1, True)


async def test_announcement_volume_and_back(kodi: FakeKodi, announcer: Announcer) -> None:
    volumes: list[int] = []
    original = kodi._Application_SetVolume

    def set_volume(volume: int) -> int:
        volumes.append(volume)
        return original(volume)

    kodi._Application_SetVolume = set_volume
    await announcer.announce(CLIP, volume=30)
    assert volumes == [30, 50]
    assert kodi.volume == 50


async def test_volume_changed_meanwhile_is_kept(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_length = 1.0
    task = asyncio.ensure_future(announcer.announce(CLIP, volume=30))
    await asyncio.sleep(0.4)
    kodi.volume = 70  # turned up on the Kodi remote… (keys aside)
    await task
    assert kodi.volume == 70


async def test_muted_kodi_is_not_interrupted(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.muted = True
    assert await announcer.announce(CLIP) is Outcome.MUTED
    assert kodi.clips == []
    assert opened(kodi) == []
    assert kodi.player_active
    assert kodi.position == 1


async def test_nothing_playing(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.player_active, kodi.position = False, -1
    assert await announcer.announce(CLIP) is Outcome.DONE
    assert kodi.clips == [CLIP]
    assert opened(kodi) == [{"item": {"file": CLIP}}]
    assert not kodi.player_active


async def test_library_movie_outside_the_queue(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.start_video_outside_queue(
        {"type": "movie", "id": 7, "file": "smb://nas/movie/7.mkv"}, seconds=754
    )
    queue = kodi.files()
    await announcer.announce(CLIP)
    assert opened(kodi)[-1] == {
        "item": {"movieid": 7},
        "options": {"resume": {"hours": 0, "minutes": 12, "seconds": 34, "milliseconds": 0}},
    }
    assert kodi.current_outside["id"] == 7
    assert kodi.position == -1  # plays outside the queue again
    assert kodi.time["minutes"] == 12
    assert kodi.files() == queue  # Kodi replaced the queue with the movie; put back


async def test_live_tv_is_switched_back(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.start_video_outside_queue({"type": "channel", "id": 3, "file": "pvr://channels/3"}, 900)
    await announcer.announce(CLIP)
    assert opened(kodi)[-1] == {"item": {"channelid": 3}}
    assert "Player.Seek" not in kodi.method_calls()


async def test_music_playlist_is_filled_again(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.music_playlist = [
        {"file": "smb://nas/music/1.flac", "label": "1", "type": "unknown"},
        {"file": "smb://nas/music/2.flac", "label": "2", "type": "unknown"},
    ]
    kodi.player_type, kodi.position, kodi.time = "audio", 1, {"minutes": 1, "seconds": 5}
    await announcer.announce(CLIP)
    assert [item["file"] for item in kodi.music_playlist] == [
        "smb://nas/music/1.flac",
        "smb://nas/music/2.flac",
    ]
    assert opened(kodi)[-1] == {"item": {"playlistid": 0, "position": 1}}
    assert kodi.player_type == "audio"
    assert kodi.position == 1


async def test_announcements_in_a_row_continue_once(kodi: FakeKodi, announcer: Announcer) -> None:
    first = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.1)
    second = asyncio.ensure_future(announcer.announce(CLIP_2))
    assert await first is Outcome.PASSED_ON
    assert await second is Outcome.DONE
    assert kodi.clips == [CLIP, CLIP_2]
    assert [p for p in opened(kodi) if "playlistid" in p["item"]] == [
        {"item": {"playlistid": 1, "position": 1}}
    ]
    assert kodi.time == AT_42_17


async def test_a_clip_that_doesnt_end_is_stopped(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_length = 100
    assert await announcer.announce(CLIP, max_duration=0.6) is Outcome.DONE
    assert kodi.position == 1
    assert kodi.time == AT_42_17


async def test_a_clip_that_doesnt_play(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_starts = False  # Kodi stops the video, then fails on the clip
    with pytest.raises(ClipError):
        await announcer.announce(CLIP, start_timeout=0.6)
    assert kodi.position == 1
    assert kodi.time == AT_42_17


async def test_stopping_the_clip_is_a_human_override(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_length = 3
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.6)
    kodi.user_stops()
    assert await task is Outcome.OVERRIDE
    assert not kodi.player_active
    assert [p for p in opened(kodi) if "playlistid" in p["item"]] == []


async def test_using_the_remote_is_a_human_override(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_length = 3.5
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(2.2)
    kodi.press_key()
    assert await task is Outcome.OVERRIDE
    assert [p for p in opened(kodi) if "playlistid" in p["item"]] == []


async def test_starting_something_else_is_a_human_override(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.clip_length = 3
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.6)
    kodi.user_plays("smb://nas/other.mkv")
    assert await task is Outcome.OVERRIDE
    assert kodi.current_outside["file"] == "smb://nas/other.mkv"


async def test_resume_dialog_of_an_add_on_is_answered(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.start_video_outside_queue({"file": "plugin://plugin.video.jellyfin/?id=1"}, 2537)
    kodi.resume_dialog = True
    assert await announcer.announce(CLIP) is Outcome.DONE
    assert "Input.Select" in kodi.method_calls()
    assert kodi.player_active
    assert kodi.time == AT_42_17


async def test_other_dialogs_are_left_alone(kodi: FakeKodi, announcer: Announcer) -> None:
    assert not await announcer._answer_resume_dialog(2537)  # nothing open
    kodi._dialog = {"item": {}, "seconds": 10, "label": "Resume from 0:00:10"}
    assert not await announcer._answer_resume_dialog(2537)
    assert "Input.Select" not in kodi.method_calls()


async def test_playback_that_doesnt_start_again_is_an_error(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.start_delay = 1000  # the player never shows up
    with pytest.raises(RestoreError):
        await announcer.announce(CLIP, start_timeout=0.6)
    assert not announcer.busy


async def test_wait_idle(kodi: FakeKodi, announcer: Announcer) -> None:
    await announcer.wait_idle()
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0)
    assert announcer.busy
    waiter = asyncio.ensure_future(announcer.wait_idle())
    await asyncio.sleep(0.2)
    assert not waiter.done()
    await task
    await asyncio.wait_for(waiter, 1)


async def test_tracks_are_chosen_once_kodi_lists_them(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.forget_streams = True
    kodi.streams_delay = 3  # the stream isn't open yet right after starting
    await announcer.announce(CLIP)
    assert (kodi.audio_index, kodi.subtitle_index, kodi.subtitle_enabled) == (1, 1, True)


async def test_repeat_doesnt_loop_the_clip(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.music_playlist = [{"file": "smb://nas/music/1.flac", "label": "1", "type": "unknown"}]
    kodi.player_type, kodi.position, kodi.time = "audio", 0, {"minutes": 1, "seconds": 5}
    kodi.music_repeat = "all"
    assert await announcer.announce(CLIP, max_duration=5) is Outcome.DONE
    assert "Player.Stop" not in kodi.method_calls()  # ended by itself, didn't loop
    assert kodi.player_type == "audio"
    assert kodi.music_repeat == "all"  # back on for the music


async def test_short_clip_not_seen_playing(kodi: FakeKodi, announcer: Announcer) -> None:
    kodi.clip_length = 0.0  # over before the first look
    assert await announcer.announce(CLIP) is Outcome.DONE
    assert kodi.position == 1
    assert kodi.time == AT_42_17


async def test_cancelled_while_waiting_kodi_still_continues(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """An automation restarted while its announcement waited for another one."""
    first = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.1)
    second = asyncio.ensure_future(announcer.announce(CLIP_2))
    await asyncio.sleep(0.1)
    second.cancel()
    await first
    await asyncio.wait_for(announcer.wait_idle(), 5)
    assert kodi.position == 1
    assert kodi.time == AT_42_17
    assert kodi.clips == [CLIP]


async def test_cancelled_while_playing_kodi_still_continues(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.clip_length = 5
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(announcer.wait_idle(), 5)
    assert kodi.player_type == "video"  # the clip was stopped
    assert kodi.position == 1
    assert kodi.time == AT_42_17

    # The next announcement notes Kodi afresh
    kodi.time = {"hours": 0, "minutes": 50, "seconds": 0, "milliseconds": 0}
    kodi.clip_length = 0.3
    await announcer.announce(CLIP_2)
    assert kodi.time["minutes"] == 50


async def test_announcements_wait_for_running_commands(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    released = asyncio.Event()

    async def command() -> None:
        async with announcer.command():
            await released.wait()

    running = asyncio.ensure_future(command())
    await asyncio.sleep(0)
    task = asyncio.ensure_future(announcer.announce(CLIP))
    await asyncio.sleep(0.2)
    assert kodi.clips == []
    released.set()
    await running
    assert await task is Outcome.DONE
    assert kodi.clips == [CLIP]


async def test_shuffled_music_continues_with_the_same_song(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    songs = [f"smb://nas/music/{n}.flac" for n in range(4)]
    kodi._Playlist_Add(0, [{"file": song} for song in songs])
    kodi.music_shuffled = True  # shown order: as added (shuffled before)
    kodi.player_type, kodi.position, kodi.time = "audio", 1, {"minutes": 1, "seconds": 5}
    await announcer.announce(CLIP)
    assert kodi.player_type == "audio"
    assert kodi._Player_GetItem(0, ["file"])["item"]["file"] == songs[1]
    assert kodi.music_shuffled


async def test_playlist_an_add_on_fills_is_left_alone(kodi: FakeKodi, announcer: Announcer) -> None:
    """After reopening a single item, an add-on (Up Next, say) builds its own playlist."""
    kodi.start_video_outside_queue({"file": "plugin://plugin.video.jellyfin/?id=1"}, 600)

    def add_on_fills() -> None:
        kodi.playlist.append({"file": "plugin://plugin.video.jellyfin/?id=2", "type": "unknown"})

    kodi.after_open_single = add_on_fills
    await announcer.announce(CLIP)
    assert [item["file"] for item in kodi.playlist] == [
        "plugin://plugin.video.jellyfin/?id=1",
        "plugin://plugin.video.jellyfin/?id=2",
    ]


async def test_cancelled_while_continuing_kodi_still_continues(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """An automation restarted while Kodi starts the movie again."""
    kodi.start_video_outside_queue(
        {"type": "movie", "id": 7, "file": "smb://nas/movie/7.mkv"}, seconds=754
    )
    queue = kodi.files()
    kodi.resume_dialog = True  # starting again takes a moment (the dialog)
    task = asyncio.ensure_future(announcer.announce(CLIP))
    for _ in range(250):
        if kodi._dialog:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(announcer.wait_idle(), 5)
    assert kodi.current_outside["id"] == 7
    assert kodi.time["minutes"] == 12
    assert kodi.files() == queue


async def test_tasks_started_by_a_command_wait_for_announcements(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Only the command's own task skips the wait, not tasks it started."""
    seen: list[bool] = []

    async def started_by_the_command() -> None:
        await asyncio.sleep(0.2)  # by now the announcement plays
        seen.append(announcer.busy)
        async with announcer.command():
            seen.append(announcer.busy)

    async with announcer.command():
        later = asyncio.ensure_future(started_by_the_command())
    await announcer.announce(CLIP)
    await later
    assert seen == [True, False]


def test_streams_are_found_by_language_and_name() -> None:
    german = Stream(1, "ger", "Deutsch 5.1")
    assert german.find_in(AUDIO) == 1
    reordered = [{**AUDIO[1], "index": 0}, {**AUDIO[0], "index": 1}]
    assert german.find_in(reordered) == 0
    assert Stream(1, "ger", "Deutsch").find_in(reordered) == 0  # only one German track
    assert Stream(1, "", "").find_in(AUDIO) == 1  # index as the last resort
    assert Stream(5, "fre", "").find_in(AUDIO) is None
    assert Stream.from_kodi({}) is None
    unnamed = [
        {"index": 0, "language": "eng", "name": ""},
        {"index": 1, "language": "eng", "name": ""},
    ]
    assert Stream(1, "eng", "").find_in(unnamed) == 1  # the noted one of two alike
