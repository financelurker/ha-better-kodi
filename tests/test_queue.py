"""Tests for the queue rules (docs/DESIGN.md, "Queue behaviour")."""

import asyncio

import pytest

from custom_components.better_kodi import queue as queue_module
from custom_components.better_kodi.queue import EnqueueMode, KodiQueue, QueueError

from .fake_kodi import FakeKodi

A, B, C, NEW = ({"file": f"plugin://x/{name}"} for name in ("a", "b", "c", "new"))


@pytest.fixture
def kodi() -> FakeKodi:
    return FakeKodi()


@pytest.fixture
def queue(kodi: FakeKodi) -> KodiQueue:
    return KodiQueue(kodi)  # type: ignore[arg-type]


async def start_queue(queue: KodiQueue, *items: dict, position: int = 0) -> None:
    await queue.replace(list(items))
    if position:
        await queue.client.call("Player.GoTo", {"playerid": 1, "to": position})


@pytest.mark.parametrize("mode", list(EnqueueMode))
async def test_idle_always_starts_fresh(
    kodi: FakeKodi, queue: KodiQueue, mode: EnqueueMode
) -> None:
    kodi.playlist = [{"file": "stale"}]
    await queue.enqueue(NEW, mode)
    assert kodi.files() == [NEW["file"]]
    assert kodi.player_active
    assert kodi.position == 0


async def test_next_inserts_after_current(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B, C, position=1)
    await queue.enqueue(NEW, EnqueueMode.NEXT)
    assert kodi.files() == [A["file"], B["file"], NEW["file"], C["file"]]
    assert kodi.position == 1


async def test_play_inserts_after_current_and_jumps(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B, C, position=1)
    await queue.enqueue(NEW, EnqueueMode.PLAY)
    assert kodi.files() == [A["file"], B["file"], NEW["file"], C["file"]]
    assert kodi.position == 2


async def test_add_appends(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B)
    await queue.enqueue(NEW, EnqueueMode.ADD)
    assert kodi.files() == [A["file"], B["file"], NEW["file"]]
    assert kodi.position == 0


async def test_replace_clears(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B, position=1)
    await queue.enqueue(NEW, EnqueueMode.REPLACE)
    assert kodi.files() == [NEW["file"]]
    assert kodi.position == 0


@pytest.mark.parametrize("mode", [EnqueueMode.NEXT, EnqueueMode.ADD])
async def test_outside_queue_appends(kodi: FakeKodi, queue: KodiQueue, mode: EnqueueMode) -> None:
    kodi.playlist = [{"file": A["file"]}]
    kodi.play_outside_queue("smb://nas/movie.mkv")
    await queue.enqueue(NEW, mode)
    assert kodi.files() == [A["file"], NEW["file"]]
    assert kodi.position == -1  # the movie keeps playing


async def test_outside_queue_play_starts_new_item(kodi: FakeKodi, queue: KodiQueue) -> None:
    kodi.playlist = [{"file": A["file"]}]
    kodi.play_outside_queue("smb://nas/movie.mkv")
    await queue.enqueue(NEW, EnqueueMode.PLAY)
    assert kodi.files() == [A["file"], NEW["file"]]
    assert kodi.position == 1


async def test_items_mark_current_and_use_known_titles(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B, position=1)
    queue.known.remember(A["file"], "Video A", "https://img/a.jpg")
    items = await queue.items()
    # Kodi only labels B with a part of its path; A has a remembered title.
    assert [item.title for item in items] == ["Video A", "b"]
    assert items[0].thumbnail == "https://img/a.jpg"
    assert [item.current for item in items] == [False, True]


async def test_remove(kodi: FakeKodi, queue: KodiQueue) -> None:
    await start_queue(queue, A, B, C)
    await queue.remove(2)
    assert kodi.files() == [A["file"], B["file"]]


async def test_remove_current_is_refused(queue: KodiQueue) -> None:
    await start_queue(queue, A, B)
    with pytest.raises(QueueError, match="playing"):
        await queue.remove(0)


async def test_remove_outside_range(queue: KodiQueue) -> None:
    await start_queue(queue, A)
    with pytest.raises(QueueError, match="outside"):
        await queue.remove(5)


@pytest.mark.parametrize(
    ("position", "to_position", "expected"),
    [(0, 2, ["b", "c", "a"]), (2, 0, ["c", "a", "b"]), (1, 1, ["a", "b", "c"])],
)
async def test_move(
    kodi: FakeKodi, queue: KodiQueue, position: int, to_position: int, expected: list[str]
) -> None:
    await start_queue(queue, A, B, C)
    await queue.move(position, to_position)
    assert kodi.files() == [f"plugin://x/{name}" for name in expected]


async def test_handover_keeps_queue_position_and_time(kodi: FakeKodi, queue: KodiQueue) -> None:
    target_kodi = FakeKodi()
    target = KodiQueue(target_kodi)  # type: ignore[arg-type]
    await start_queue(queue, A, B, C, position=1)
    kodi.time = {"hours": 0, "minutes": 12, "seconds": 34, "milliseconds": 0}

    snapshot = await queue.snapshot()
    await target.restore(snapshot)

    assert target_kodi.files() == kodi.files()
    assert target_kodi.position == 1
    assert target_kodi.seeks == [{"time": kodi.time}]


async def test_handover_of_item_outside_queue(kodi: FakeKodi, queue: KodiQueue) -> None:
    target_kodi = FakeKodi()
    kodi.play_outside_queue("smb://nas/movie.mkv")
    snapshot = await queue.snapshot()
    await KodiQueue(target_kodi).restore(snapshot, resume=False)  # type: ignore[arg-type]
    assert target_kodi.files() == ["smb://nas/movie.mkv"]
    assert target_kodi.seeks == []


async def test_handover_needs_playback(queue: KodiQueue) -> None:
    with pytest.raises(QueueError, match="Nothing is playing"):
        await queue.snapshot()


async def test_quick_second_add_while_kodi_is_starting(kodi: FakeKodi, queue: KodiQueue) -> None:
    """Kodi's player appears only a while after Player.Open."""
    kodi.start_delay = 3
    await queue.enqueue(A, EnqueueMode.ADD)
    assert not kodi.player_active
    await queue.enqueue(B, EnqueueMode.ADD)
    await queue.enqueue(NEW, EnqueueMode.NEXT)
    assert kodi.files() == [A["file"], NEW["file"], B["file"]]


async def test_play_while_kodi_is_starting(kodi: FakeKodi, queue: KodiQueue) -> None:
    kodi.start_delay = 5
    await queue.enqueue(A, EnqueueMode.PLAY)
    await queue.enqueue(NEW, EnqueueMode.PLAY)
    assert kodi.files() == [A["file"], NEW["file"]]
    assert kodi.position == 1


async def test_concurrent_adds_keep_every_item(kodi: FakeKodi, queue: KodiQueue) -> None:
    kodi.start_delay = 2
    await asyncio.gather(*(queue.enqueue(item, EnqueueMode.ADD) for item in (A, B, C)))
    assert sorted(kodi.files()) == sorted([A["file"], B["file"], C["file"]])


async def test_start_window_expires(
    kodi: FakeKodi, queue: KodiQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    kodi.start_delay = 1000  # Kodi never starts
    await queue.enqueue(A, EnqueueMode.ADD)
    monkeypatch.setattr(queue_module, "START_WINDOW", 0)
    await queue.enqueue(B, EnqueueMode.ADD)
    assert kodi.files() == [B["file"]]


async def test_handover_retries_seek(
    kodi: FakeKodi, queue: KodiQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "POLL_INTERVAL", 0)
    target_kodi = FakeKodi()
    target_kodi.start_delay = 2
    target_kodi.seek_refusals = 2
    await start_queue(queue, A, B)
    kodi.time = {"hours": 1, "minutes": 0, "seconds": 5, "milliseconds": 0}
    assert await KodiQueue(target_kodi).restore(await queue.snapshot())  # type: ignore[arg-type]
    assert target_kodi.seeks == [{"time": kodi.time}]


async def test_handover_gives_up_seeking(
    kodi: FakeKodi, queue: KodiQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "POLL_INTERVAL", 0)
    monkeypatch.setattr(queue_module, "SEEK_TIMEOUT", 0)
    target_kodi = FakeKodi()
    target_kodi.seek_refusals = 100
    await start_queue(queue, A)
    kodi.time = {"hours": 0, "minutes": 1, "seconds": 0, "milliseconds": 0}
    assert not await KodiQueue(target_kodi).restore(await queue.snapshot())  # type: ignore[arg-type]
    assert target_kodi.player_active


async def test_titles_of_entries_added_by_other_apps(kodi: FakeKodi) -> None:
    """Cast Kodi adds a YouTube path; Kodi only labels it "play"."""
    looked_up: list[str] = []
    youtube = "plugin://plugin.video.youtube/play/?video_id=dQw4w9WgXcQ"

    async def lookup(file: str):
        looked_up.append(file)
        return ("Real title", "https://img/t.jpg") if file == youtube else None

    queue = KodiQueue(kodi, lookup)  # type: ignore[arg-type]
    await kodi.call(
        "Playlist.Add",
        {
            "playlistid": 1,
            "item": [{"file": youtube}, {"file": "plugin://x/unknown"}, {"movieid": 3}],
        },
    )
    items = await queue.items()
    assert [item.title for item in items] == ["Real title", "unknown", "movie 3"]
    assert items[0].thumbnail == "https://img/t.jpg"

    await queue.items()
    assert sorted(looked_up) == sorted([youtube, "plugin://x/unknown"])  # each once


async def test_title_lookup_timeout_is_retried(
    kodi: FakeKodi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "TITLE_LOOKUP_TIMEOUT", 0.01)
    calls = 0

    async def slow(file: str):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)

    queue = KodiQueue(kodi, slow)  # type: ignore[arg-type]
    await kodi.call("Playlist.Add", {"playlistid": 1, "item": {"file": "plugin://x/a"}})
    assert [item.title for item in await queue.items()] == ["a"]
    await queue.items()
    assert calls == 2


async def test_titled_addon_entries_are_not_looked_up(kodi: FakeKodi) -> None:
    """A YouTube playlist played by the add-on fills the queue with titled entries."""
    looked_up: list[str] = []

    async def lookup(file: str):
        looked_up.append(file)
        return ("Looked up", None)

    queue = KodiQueue(kodi, lookup)  # type: ignore[arg-type]
    kodi.playlist = [
        {
            "file": f"plugin://plugin.video.youtube/play/?video_id=v{n}",
            "label": f"Video {n}",
            "title": f"Video {n}",
            "type": "unknown",
        }
        for n in range(10)
    ]
    assert [item.title for item in await queue.items()] == [f"Video {n}" for n in range(10)]
    assert looked_up == []


async def test_failed_lookup_is_retried_later(
    kodi: FakeKodi, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [None, ("Back online", None)]

    async def lookup(file: str):
        return answers.pop(0)

    queue = KodiQueue(kodi, lookup)  # type: ignore[arg-type]
    await kodi.call("Playlist.Add", {"playlistid": 1, "item": {"file": "plugin://x/play"}})
    assert [item.title for item in await queue.items()] == ["play"]
    assert [item.title for item in await queue.items()] == ["play"]  # not asked again yet
    monkeypatch.setattr(queue_module, "LOOKUP_RETRY_AFTER", -1)
    assert [item.title for item in await queue.items()] == ["Back online"]


async def test_broken_lookup_does_not_break_the_queue(kodi: FakeKodi) -> None:
    async def lookup(file: str):
        raise ValueError("odd answer")

    queue = KodiQueue(kodi, lookup)  # type: ignore[arg-type]
    await kodi.call("Playlist.Add", {"playlistid": 1, "item": {"file": "plugin://x/play"}})
    assert [item.title for item in await queue.items()] == ["play"]


async def test_lookups_are_limited_and_not_doubled(kodi: FakeKodi) -> None:
    running = 0
    most = 0
    calls: list[str] = []

    async def lookup(file: str):
        nonlocal running, most
        calls.append(file)
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return (file.upper(), None)

    queue = KodiQueue(kodi, lookup)  # type: ignore[arg-type]
    await kodi.call(
        "Playlist.Add",
        {"playlistid": 1, "item": [{"file": f"plugin://x/{n}"} for n in range(40)]},
    )
    await asyncio.gather(queue.items(), queue.items())
    assert most <= queue_module.MAX_PARALLEL_LOOKUPS
    assert len(calls) == queue_module.MAX_LOOKUPS_PER_READ  # and nothing twice
    await queue.items()
    assert len(calls) == 40
