"""Tests for announcements: interrupt Kodi, play a clip, continue where it was."""

import asyncio
from collections.abc import Generator
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
from custom_components.better_kodi.kodi_client import KodiConnectionError

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
    monkeypatch.setattr(announce, "TAIL_CHECK_INTERVAL", 0.1)


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
def announcer(kodi: FakeKodi) -> Generator[Announcer]:
    announcer = Announcer(kodi)  # FakeKodi answers like KodiClient
    if kodi.websocket:
        kodi.notify = announcer.on_notification
        kodi.notifications_connected = True
    yield announcer
    announcer.close()


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


MOVIE_7 = {"type": "movie", "id": 7, "file": "smb://nas/movie/7.mkv"}
AT_12_34 = {"hours": 0, "minutes": 12, "seconds": 34, "milliseconds": 0}


async def test_movie_outside_the_queue_kodi_goes_on_with_the_queue(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Case A: the queue is still Kodi's current playlist; Kodi plays it after the movie."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754, queue_current=True)
    queue = kodi.files()
    await announcer.announce(CLIP)
    assert opened(kodi)[-1] == {"item": {"movieid": 7}, "options": {"resume": AT_12_34}}
    assert kodi.current_outside["id"] == 7
    assert kodi.position == -1  # outside the queue again
    assert kodi.time == AT_12_34
    assert kodi.files() == queue
    kodi.video_ends()
    assert kodi.player_active
    assert kodi.position == 0  # as it would have without the announcement


async def test_movie_outside_the_queue_kodi_stops_after_it(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Case B: Kodi would stop after the movie; it still does, and the queue is as before."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    queue = kodi.files()
    await announcer.announce(CLIP)
    # Kodi can only start the movie as a playlist entry: the queue goes in front of it.
    assert kodi.files() == [*queue, MOVIE_7["file"]]
    assert kodi.position == len(queue)
    assert kodi.time == AT_12_34
    kodi.video_ends()
    assert not kodi.player_active  # stopped after the movie, as before
    for _ in range(100):
        if kodi.files() == queue:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == queue  # the movie's entry is gone again


async def test_reopened_movie_entry_stays_while_it_plays(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    queue = kodi.files()
    await announcer.announce(CLIP)
    await asyncio.sleep(0.3)  # a few checks
    assert kodi.files() == [*queue, MOVIE_7["file"]]
    kodi.user_stops()
    for _ in range(100):
        if kodi.files() == queue:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == queue


async def test_movie_outside_the_queue_with_an_add_on_path(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Kodi shows a playing add-on item by its stream URL; it is removed all the same."""
    item = "plugin://plugin.video.jellyfin/?id=1"
    kodi.start_video_outside_queue({"file": item}, seconds=754)
    kodi.resolved = {item: "http://jellyfin.local/Videos/1/stream?api_key=x"}
    queue = kodi.files()
    await announcer.announce(CLIP)
    assert kodi.position == len(queue)
    assert kodi.files()[:-1] == queue
    kodi.video_ends()
    for _ in range(100):
        if kodi.files() == queue:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == queue


async def test_users_own_entries_are_never_removed(kodi: FakeKodi, announcer: Announcer) -> None:
    """The user replaced the queue while the movie played; it contains the same movie."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    await announcer.announce(CLIP)
    kodi.user_stops()
    kodi._Playlist_Clear(1)
    kodi._Playlist_Add(1, [{"movieid": 7}, {"file": "smb://nas/c.mkv"}])
    users = kodi.files()
    await asyncio.sleep(0.5)  # several checks
    assert kodi.files() == users


async def test_entries_added_after_the_movie_stay(kodi: FakeKodi, announcer: Announcer) -> None:
    """A link added while the movie plays: Kodi plays it after the movie, the movie goes."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    queue = kodi.files()
    await announcer.announce(CLIP)
    kodi._Playlist_Add(1, {"file": "https://cdn.example/b.mp4"})
    kodi.video_ends()
    assert kodi.player_active  # plays the added link
    for _ in range(100):
        if kodi.files() == [*queue, "https://cdn.example/b.mp4"]:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == [*queue, "https://cdn.example/b.mp4"]
    assert kodi.position == len(queue)  # still the link


async def test_movie_outside_a_shuffled_queue(kodi: FakeKodi, announcer: Announcer) -> None:
    """With shuffle or repeat on, Kodi goes on with the queue after the movie (a known limit)."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    kodi.video_repeat = "all"
    queue = kodi.files()
    await announcer.announce(CLIP)
    assert kodi.files() == queue
    assert kodi.position == -1


async def test_an_entry_kodi_refuses_doesnt_cost_the_queue(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.playlist[0] = {"file": "/media/usb/a.mkv", "label": "a", "type": "unknown"}
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    first, second = kodi.files()
    kodi.unavailable = {first}  # the USB drive is gone
    await announcer.announce(CLIP)
    assert kodi.files() == [second, MOVIE_7["file"]]
    assert kodi.position == 1
    kodi.video_ends()
    for _ in range(100):
        if kodi.files() == [second]:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == [second]


async def test_tidying_up_doesnt_hold_up_announcements(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Kodi hangs on the tidy-up check: an announcement doesn't wait for it."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    await announcer.announce(CLIP)
    call = kodi.call
    hanging = asyncio.Event()

    async def hang_on_ping(method: str, params: Any = None) -> Any:
        if method == "Playlist.GetItems" and kodi.player_active and not hanging.is_set():
            hanging.set()
            await asyncio.sleep(5)
        return await call(method, params)

    kodi.call = hang_on_ping
    await asyncio.wait_for(hanging.wait(), 2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await announcer.announce(CLIP_2)
    assert loop.time() - started < 2.5  # the clip plays 0.6 s; the check would hang 5 s


async def test_kodi_gone_while_putting_the_queue_back(kodi: FakeKodi, announcer: Announcer) -> None:
    """Kodi stops answering after the first entry: its entry still goes once it answers."""
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    first, _ = kodi.files()
    call = kodi.call
    inserts = 0

    async def gone_after_one_insert(method: str, params: Any = None) -> Any:
        nonlocal inserts
        if method == "Playlist.Insert":
            inserts += 1
            if inserts == 2:
                raise KodiConnectionError("Playlist.Insert: gone")
        return await call(method, params)

    kodi.call = gone_after_one_insert
    await announcer.announce(CLIP)
    assert kodi.files() == [first, MOVIE_7["file"]]
    kodi.video_ends()
    for _ in range(100):
        if kodi.files() == [first]:
            break
        await asyncio.sleep(0.05)
    assert kodi.files() == [first]


async def test_closing_stops_a_watcher_whose_check_was_cancelled(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    kodi.start_video_outside_queue(MOVIE_7, seconds=754)
    await announcer.announce(CLIP)
    (watcher,) = announcer._tail_tasks
    for _ in range(100):
        if announcer._tail_checks:
            break
        await asyncio.sleep(0.01)
    for check in list(announcer._tail_checks):
        check.cancel()  # as an announcement does
    announcer.close()  # in the same step
    await asyncio.sleep(0.3)
    assert watcher.done()


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
    kodi.start_video_outside_queue(MOVIE_7, seconds=754, queue_current=True)
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


async def test_video_clip_leaves_the_queue_alone(kodi: FakeKodi, announcer: Announcer) -> None:
    doorbell = "http://ha.local:8123/local/doorbell.mp4"
    kodi.video_clips = {doorbell}
    queue = kodi.files()
    assert await announcer.announce(doorbell) is Outcome.DONE
    assert kodi.clips == [doorbell]
    assert kodi.files() == queue
    assert kodi.position == 1
    assert kodi.time == AT_42_17


async def test_a_command_doesnt_slip_between_two_announcements(
    kodi: FakeKodi, announcer: Announcer
) -> None:
    """Two announcements in a row from one script; a command waits for both."""
    log: list[str] = []

    async def script() -> None:
        await announcer.announce(CLIP)
        log.append("first done")
        await announcer.announce(CLIP_2)
        log.append("second done")

    async def command() -> None:
        async with announcer.command():
            log.append(f"command, busy={announcer.busy}")

    running = asyncio.ensure_future(script())
    await asyncio.sleep(0.1)
    waiting = asyncio.ensure_future(command())
    await asyncio.gather(running, waiting)
    assert "command, busy=True" not in log


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
