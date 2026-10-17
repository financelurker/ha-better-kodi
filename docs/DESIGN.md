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
- Better Kodi creates its own `media_player` entity. It mirrors state and attributes of the Kodi entity and forwards standard commands (play, pause, volume, seek, power, …) to it with `media_player.*` actions. Fixes in the built-in integration therefore reach Better Kodi automatically, and power automations on `kodi.turn_on` / `kodi.turn_off` keep working.
- Browsing the Kodi library is forwarded with the public `media_player.browse_media` action. Better Kodi adds a **Queue** folder at the top.
- For everything the built-in integration cannot do, Better Kodi talks to Kodi directly over JSON-RPC (HTTP for requests, WebSocket for live notifications). This uses Better Kodi's own small client, no third-party library.

### Connection data

When Better Kodi is set up, it reads host, ports and credentials from the config entry of the selected Kodi entity and pre-fills them. They are stored in Better Kodi's own entry and can be changed in the setup step and later with *Reconfigure*.

Reading another integration's config entry data couples us to its internal keys. Accepted: if Core changes them, pre-filling breaks until Better Kodi is updated; already stored entries keep working.

### Device, visibility and Assist

- The Better Kodi entity is attached to the existing Kodi device (`entity.device_entry`). Since Home Assistant 2026.10 a device belongs to exactly one config entry, so the device page lists the Kodi integration as owner and shows the Better Kodi entity among its entities. Better Kodi is not listed as a second integration on the device.
- Optional (default on): the Kodi entity is hidden when Better Kodi is set up and shown again when Better Kodi is removed. Only a hide done by Better Kodi is undone.
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

If Kodi is playing something that was not started from the queue (for example a movie picked on the TV), `next` and `add` append to the queue, and `play` starts the new item from the queue.

Library items from the media browser (`movie`, `episode`, `musicvideo`) can be queued by their Kodi ID. Other library items (shows, seasons, channels, directories) are forwarded to the Kodi entity and only support playing now.

## URL resolvers

A resolver turns a page URL into something Kodi can play. They are tried in order; the first match wins.

| Site | Kodi add-on needed | Result | Queueable |
| --- | --- | --- | --- |
| YouTube video, Shorts, live, `youtu.be` | `plugin.video.youtube` | `plugin://plugin.video.youtube/play/?video_id=…` | yes |
| YouTube playlist | `plugin.video.youtube` | `plugin://plugin.video.youtube/play/?playlist_id=…&play=1` | play now only |
| Rumble | none | Direct video URL from Rumble's embed API (resolved in Home Assistant, like Cast Kodi does) | yes |
| Jellyfin web link (`…#/details?id=…`) | `plugin.video.jellyfin` (Jellyfin for Kodi) | `plugin://plugin.video.jellyfin/?mode=play&id=…` | yes |
| Jellyfin web link | `plugin.video.jellycon` (JellyCon) | `plugin://plugin.video.jellycon/?mode=PLAY&item_id=…` | play now only (JellyCon starts playback itself) |
| Direct media URL (`.mp4`, `.mkv`, `.m3u8`, …) and Home Assistant media sources | none | passed through | yes |
| Anything else | `plugin.video.sendtokodi` | `plugin://plugin.video.sendtokodi/?<url>` | yes |

If the given text contains spaces (shared text such as "Look at this: https://…"), the first `http(s)://` link in it is used.

Resolvers that need an add-on are only active when that add-on is installed and enabled in Kodi (checked with `Addons.GetAddons`). Without a matching resolver, Better Kodi passes the URL to Kodi unchanged.

Titles and thumbnails: YouTube titles come from YouTube's oEmbed endpoint (no API key), Rumble titles from its embed API. They are remembered for the Queue folder, because Kodi shows add-on items only as paths until they are played.

## Handover

`better_kodi.handover` moves playback from one Better Kodi entity to another:

1. Read the source's queue (file paths, so it also works when the two Kodis have separate libraries), current position and playback time.
2. Load the queue on the target, start at the same position, then seek to the same time.
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
- No code is copied from Home Assistant Core or other projects. Facts such as add-on URL formats and Rumble's API are documented sources, not copied code.
- Minimum Home Assistant version: 2026.10.
- License: MIT.
