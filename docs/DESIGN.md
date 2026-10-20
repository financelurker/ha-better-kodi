# Better Kodi – Design

This document records how Better Kodi works and why. It is the reference for decisions; change it together with the code.

## Goal

Add what the built-in Kodi integration lacks for casting-style use, without replacing it:

- Play a URL now, play it next, or add it to the end of Kodi's queue (like the Cast Kodi browser extension)
- See and manage the queue in Home Assistant
- Hand playback over from one Kodi to another

## Architecture

Better Kodi is a **wrapper** around the built-in Kodi integration, in the spirit of Better Thermostat.

- The built-in Kodi integration stays configured and unchanged. Better Kodi depends on it.
- One Better Kodi config entry wraps one Kodi `media_player` entity. Several Kodi instances are set up one by one.
- Better Kodi creates its own `media_player` entity. It mirrors state and attributes of the Kodi entity and forwards standard commands (play, pause, volume, seek, power, …) to it with `media_player.*` actions. Fixes in the built-in integration therefore reach Better Kodi automatically. Power commands are forwarded too by default, so automations on `kodi.turn_on` / `kodi.turn_off` keep working; optionally Better Kodi switches Kodi on and off itself (see *Switching Kodi on and off*).
- Browsing the Kodi library is forwarded with the public `media_player.browse_media` action. Better Kodi adds a **Queue** folder at the top.
- For everything the built-in integration cannot do, Better Kodi talks to Kodi directly over JSON-RPC (HTTP for requests, WebSocket for live notifications). This uses Better Kodi's own small client, no third-party library.

### Connection data

When Better Kodi is set up, it reads host, ports and credentials from the config entry of the selected Kodi entity and pre-fills them. They are stored in Better Kodi's own entry and can be changed in the setup step and later with *Reconfigure*.

Reading another integration's config entry data couples us to its internal keys. Accepted: if Core changes them, pre-filling breaks until Better Kodi is updated; already stored entries keep working.

### Kodi switched off, wrong password, Kodi entity gone

- Better Kodi starts even when Kodi doesn't answer, like the Kodi integration does. Otherwise `turn_on` (Wake on LAN or the user's Kodi turn-on automation) would be unavailable exactly when it is needed. Add-ons and the queue are read once Kodi answers.
- If Kodi rejects the credentials, setup fails with a re-authentication request.
- If the Kodi entity is removed, the entry fails to set up with an error asking to remove Better Kodi or set up Kodi again. A renamed Kodi entity is followed (the entry reloads).

## Device, visibility and Assist

- The Better Kodi entity is attached to the existing Kodi device (`entity.device_entry`). Since Home Assistant 2026.10 a device belongs to exactly one config entry, so the device page lists the Kodi integration as owner and shows the Better Kodi entity among its entities. Better Kodi is not listed as a second integration on the device.
- Optional (default on): the Kodi entity is hidden when Better Kodi is set up and shown again when Better Kodi is removed. Only a hide done by Better Kodi is undone: nothing is unhidden if the option was off, and a hide by the user is never touched.
- Assist: on first setup, the Kodi entity's exposure settings and aliases are copied to the Better Kodi entity and the Kodi entity is unexposed. On removal, the exposure settings are copied back.

## Queue behaviour

Better Kodi only works with Kodi's **video** playlist (`playlistid 1`). The music playlist is left alone; music is handled by Better Media Player and Music Assistant.

`media_player.play_media` with `enqueue`:

| `enqueue` | Something is playing from the queue | Nothing is playing |
| --- | --- | --- |
| `play` (default) | Insert after the current item and jump to it; the rest of the queue stays | Clear the queue and play |
| `next` | Insert after the current item | Clear the queue and play |
| `add` | Append to the end | Clear the queue and play |
| `replace` | Clear the queue and play | Clear the queue and play |

Kodi answers `Player.Open` at once but starts the player later; add-on items can take seconds. For 20 seconds after Better Kodi starts the queue, or until the player appears, the queue counts as playing at the started position, so a quick second `add` does not wipe the queue. All changing queue operations run one after another (a lock per Kodi), because Home Assistant runs service calls concurrently.

If Kodi is playing something that was not started from the queue (for example a movie picked on the TV), `next` and `add` append to the queue, and `play` starts the new item from the queue.

Library items from the media browser (`movie`, `episode`, `musicvideo`) can be queued by their Kodi ID. Other library items (shows, seasons, channels, directories) are forwarded to the Kodi entity and only support playing now.

## URL resolvers

A resolver turns a page URL into something Kodi can play. They are tried in order; the first match wins.

| Site | Kodi add-on needed | Result | Queueable |
| --- | --- | --- | --- |
| YouTube video, Shorts, live, `youtu.be` | `plugin.video.youtube` | `plugin://plugin.video.youtube/play/?video_id=…` | yes |
| YouTube playlist | `plugin.video.youtube` | `plugin://plugin.video.youtube/play/?playlist_id=…&order=default&action=play` | play now only |
| Jellyfin web link (`…#/details?id=…`) | `plugin.video.jellyfin` (Jellyfin for Kodi) | `plugin://plugin.video.jellyfin/?mode=play&id=…` | yes |
| Jellyfin web link | `plugin.video.jellycon` (JellyCon) | `plugin://plugin.video.jellycon/?mode=PLAY&item_id=…` | play now only (JellyCon starts playback itself) |
| Direct media URL (`.mp4`, `.mkv`, `.m3u8`, …) and Home Assistant media sources | none | passed through | yes |
| Anything else | `plugin.video.sendtokodi` | `plugin://plugin.video.sendtokodi/?<url>` | yes |

If the given text contains spaces (shared text such as "Look at this: https://…"), the first `http(s)://` link in it is used.

Installed and enabled add-ons are read with `Addons.GetAddons` (cached for 10 minutes, retried while Kodi is off). If a link is recognized but its add-on is missing, the resolver fails: YouTube links then fall back to SendToKodi if it is installed; Jellyfin links never do, because they need the Jellyfin login that only the Jellyfin add-ons have. Unrecognized `http(s)` links go to SendToKodi if installed, otherwise to Kodi unchanged.

"Play now only" items (YouTube playlists, JellyCon) are always played with `replace`: their add-ons fill the video playlist themselves and clear it first. `next` and `add` are refused with an error.

Titles and thumbnails: Kodi labels an add-on item that hasn't been played yet with a part of its path (for example `play` for a YouTube add-on path). Better Kodi therefore names queue entries itself, like Cast Kodi does:

- Links played through Better Kodi: YouTube titles from YouTube's oEmbed endpoint (no API key).
- Entries added by other apps (Cast Kodi, Yatse, …) or before a restart: when the queue is read, YouTube add-on paths (`video_id`, `playlist_id`, `uri2addon`) and SendToKodi paths with a YouTube link are looked up via oEmbed.

Only entries without a title are looked up: Kodi leaves `title` empty for unplayed paths, while entries an add-on filled in (for example the items of a YouTube playlist) keep their title. At most 25 entries are looked up per read, 4 at a time, and the same file is never looked up twice at once. Covers in the Queue folder, in this order: an image Better Kodi remembered (YouTube oEmbed, Jellyfin), Kodi's image of the entry (Kodi always returns `image://<encoded value>/`; it is loaded through Home Assistant's image proxy, which asks Kodi, so header options and images only Kodi can reach keep working; skin placeholders like `image://DefaultVideo.png/` are dropped), YouTube's standard image `https://i.ytimg.com/vi/<id>/hqdefault.jpg` for YouTube entries, and for the playing entry first of all the Kodi entity's picture (Kodi keeps the image of what plays on the player, not on the playlist entry; unplayed add-on entries have no image at all).

With the option *Look up titles online* off (default on), YouTube's oEmbed endpoint is not asked and the `i.ytimg.com` image is not used; Home Assistant then makes no requests to YouTube for Better Kodi. Media-source backends (Jellyfin) are not affected: they ask the user's own server.

Results are kept in memory (up to 2000). A lookup that found nothing, failed or answered oddly is tried again after 10 minutes; a timeout on the next read. Kodi label formatting such as `[B]…[/B]` is removed.

## Switching Kodi on and off

Options *Switch Kodi on with* and *Switch Kodi off with*, both defaulting to the Kodi integration's behaviour, so nothing changes for existing setups:

- **On, Kodi automation** (default): `turn_on` is forwarded to the Kodi entity, which only fires `kodi.turn_on` for the user's "Kodi turned on" automation.
- **On, Wake on LAN**: Better Kodi sends the magic packet itself (6 × `0xFF`, then the MAC address 16 times, UDP port 9, to the broadcast address option, default `255.255.255.255`). The MAC address is read from Kodi (`XBMC.GetInfoLabels` `Network.MacAddress`, which answers `Busy` at first, so it is asked up to three times) when the options are opened while Kodi is on, and can be typed in. The options refuse Wake on LAN without a valid MAC address.
- **Off, Kodi automation** (default): `turn_off` is forwarded (`kodi.turn_off`).
- **Off, Suspend / Hibernate / Shut down / Quit Kodi**: `System.Suspend`, `System.Hibernate`, `System.Shutdown`, `Application.Quit`. For the first three, `System.GetProperties` (`cansuspend`, …) is checked first, and a clear error is raised if Kodi may not do it. Kodi may drop the connection before it answers; that counts as done. If the Kodi entity is already `off`, nothing is sent.

Commands sent to the hidden Kodi entity itself still go through the Kodi integration (its events).

## Waking Kodi before sending

Before `play_media` (which also covers the buttons, media browser choices and queue jumps) and on the destination of a handover: if the Kodi entity's state is `off` and the option *Wake Kodi before sending* is on (default), Better Kodi switches Kodi on as configured above. With Wake on LAN the packet is repeated every 10 seconds while waiting (it is safe to repeat; a turn-on automation is run once only, because it may toggle). Better Kodi then waits until the Kodi entity's state is something other than `off`, `unavailable` or `unknown` (option *Wait at most*, default 120 s, 10–900 s), and then up to 30 s more until Kodi answers `JSONRPC.Ping`, because the Kodi integration may report Kodi before its web server answers. The add-on list is re-read afterwards. If Kodi does not wake in time, the call fails with an error naming the Kodi entity; nothing is sent. Changing the options reloads the entry.

- With the automation setting: the Kodi integration's `turn_on` only fires the `kodi.turn_on` event, which the user's "Kodi turned on" automation listens for. If nothing listens at all, Better Kodi fails at once with an error asking to create that automation, instead of waiting for nothing. Event listeners can't be told apart by Kodi, so an automation for *another* Kodi also counts; then the timeout applies. Right after Home Assistant starts or while automations reload, triggers are briefly detached, so a send in that moment may report a missing automation.
- One wake per Kodi: calls that arrive while Kodi is being woken wait for that same wake (a shared task, shielded so one caller giving up doesn't cancel it for the others). The automation runs once; a power toggle would otherwise switch the device off again. The task is kept per Kodi entity outside the config entry, so a reload (for example after changing options) doesn't start a second wake; it is a background task, so it is cancelled when Home Assistant stops, and its waiters then get a clear error.
- Checks that need no Kodi (unknown choice, items that can't be queued) run before waking, so a bad request doesn't switch Kodi on.
- Handover wakes the destination before taking the snapshot, so position and time are current when the destination starts.
- The link buttons refuse to send while Better Kodi's player is unavailable, because Home Assistant silently skips service calls to unavailable entities (and the link would be cleared as if sent).

## Controls in the user interface

Home Assistant's own interface (media browser, more-info dialog, media control card) never passes `enqueue`, for any player. Better Kodi therefore adds:

- A **Link** text entity and four buttons (play now, next, add, replace) on the Kodi device. Option *Link field and buttons* (default on); when off, the entities are removed from the entity registry on setup. A button calls `media_player.play_media` on Better Kodi's player with the link and clears the field on success. The link lives in memory only; it is empty after a restart.
- **Choices in the media browser** (an `enqueue` passed together with a choice ID is ignored; the choice wins): every playable leaf that can be queued (Kodi library movies, episodes and music videos, media source items, links) is returned as expandable *and* playable, with the ID `better_kodi://choose/<type>/<id>`. The frontend opens expandable items on click and plays them with the play button, so a click shows four entries `better_kodi://enqueue/<mode>/<type>/<id>`, and the play button plays at once. Type and ID are URL-encoded. Title and thumbnail of the wrapped item are kept in memory (up to 1000); an unknown choices ID still works, with a generic title. Option *Choices in the media browser* (default on); when off, items are returned unchanged. Choice IDs stored earlier keep working either way.

## Media-source backends

Items from Home Assistant's media browser are resolved with `media_source.async_resolve_media` to a URL. A **media-source backend** (`backends/`, interface `MediaSourceBackend` in `backends/base.py`) can turn the URL of one media server into a better Kodi item:

- `recognizes(url)`: no I/O; the first backend in `BACKENDS` that recognizes the URL handles it.
- `async_kodi_item(context, url)`: returns a `BackendItem` (Kodi playlist item plus optional title and image for the queue), or `None` to queue the URL unchanged.
- `async_title(context, file)`: title and image of a queue entry with such a URL (entries from other apps or from before a restart).

`BackendContext` gives a certificate-checking web session, the Kodi client and the (cached) list of enabled add-ons. Exceptions from a backend are logged and never break playing; the URL is then queued unchanged. Backends run before the YouTube title lookup and are not affected by *Look up titles online* (they talk to the user's own server).

### Jellyfin backend

Home Assistant's Jellyfin integration resolves items to `…/Videos/<id>/stream?static=true&api_key=…`. Kodi's JSON-RPC API cannot pass a title with `Playlist.Add`, and Kodi labels the URL "stream". Better Kodi reads the item from `GET <server>/Items?ids=<id>&fields=Path`, with the key from that URL (`ApiKey` or `api_key`) sent as `Authorization: MediaBrowser Token="…"` header to that same server only, and queues:

1. the Kodi library item, if Kodi has it: Jellyfin for Kodi in add-on mode stores files as `plugin://plugin.video.jellyfin/<library>/?filename=…&id=<jellyfin id>&…`, found with a `filename contains "id=<id>"` filter; in direct-path mode Kodi's `filename` field holds the file name only, so library files with Jellyfin's file name are fetched and exactly one of them must also be in a folder of the same name (`S01E01.mkv` alone is not enough);
2. otherwise, with `plugin.video.jellyfin` installed, `plugin://plugin.video.jellyfin/?mode=play&id=<id>`;
3. otherwise the stream URL.

The title (`Show – S01E02 – Episode` or `Movie (Year)`) and the image `<server>/Items/<id>/Images/Primary` (no API key) are remembered for the Queue folder. Jellyfin stream URLs queued by other apps are named the same way when the queue is read.

## Handover

`better_kodi.handover` moves playback from one Better Kodi entity to another:

The service field is called `destination`, not `target`, so it does not get mixed up with the action's own target.

1. Read the source's queue (file paths, so it also works when the two Kodis have separate libraries), current position and playback time.
2. Load the queue on the target, start at the same position, then seek to the same time. A fresh player may refuse to seek at first; the seek is retried for up to 15 seconds. If it still fails, the handover finishes anyway (the item starts from the beginning) and a warning is logged.
3. Stop the source (unless `keep_source_playing` is set).

If the source plays something that is not from the queue, only that item is handed over.

## Services

| Service | Purpose |
| --- | --- |
| `media_player.play_media` | With `enqueue` and URL resolving as above |
| `media_player.clear_playlist` | Clears the video queue |
| `better_kodi.get_queue` | Returns the queue with positions, titles and the current item |
| `better_kodi.remove_from_queue` | Removes an item by position |
| `better_kodi.move_queue_item` | Moves an item to another position |
| `better_kodi.handover` | Hands playback over to another Better Kodi entity |
| `better_kodi.call_method` | Calls any Kodi JSON-RPC method and returns the response |

## Scope decisions

- No upstreaming to Home Assistant Core is planned. Anyone may take the ideas there.
- No code is copied from Home Assistant Core or other projects. Facts such as add-on URL formats and the Wake on LAN packet format are documented sources, not copied code.
- Rumble was removed until it works: the embed-API approach produced unusable streams in a real test. Rumble links now go to SendToKodi like any other page.
- Minimum Home Assistant version: 2026.10.
- License: MIT.
