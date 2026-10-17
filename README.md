# Better Kodi

Enhanced Kodi integration for Home Assistant: play, play next or enqueue URLs (YouTube & more) like Cast Kodi, with queue management in the media browser.

Better Kodi does not replace the built-in Kodi integration. It sits on top of it, like Better Thermostat does for thermostats: you pick a Kodi media player, and Better Kodi gives you a media player that does everything the original does, plus:

- **Play links** from YouTube, Rumble, Jellyfin and direct media URLs: play now, play next, or add to the end of the queue (`enqueue: play | next | add | replace`)
- **The queue in the media browser**, updated live: see what's coming, jump to an item
- **Queue services**: read the queue, remove and move items
- **Handover**: continue the current video, with its whole queue and at the same position, on another Kodi
- **`call_method` with a response**: call any Kodi JSON-RPC method and use the result in your script

> Kodi's **music** queue is left alone on purpose. For music, see [Better Media Player](https://github.com/financelurker/ha-better-media-player) and Music Assistant.

## Requirements

- Home Assistant 2026.10 or newer
- The built-in [Kodi integration](https://www.home-assistant.io/integrations/kodi/), set up for each Kodi
- In Kodi, under **Settings → Services → Control**:
  - **Allow remote control via HTTP** (already on if the Kodi integration works)
  - **Allow remote control from applications on other systems**, for live queue updates
- The Kodi add-ons for the sites you want to use (see [Supported links](#supported-links))

## Installation

### HACS

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/financelurker/ha-better-kodi` with category **Integration**.
3. Install **Better Kodi** and restart Home Assistant.

### Manual

Copy `custom_components/better_kodi` into the `custom_components` folder of your Home Assistant configuration and restart.

## Setup

**Settings → Devices & services → Add integration → Better Kodi**, once per Kodi.

1. Choose the Kodi media player. Keep **Hide the Kodi media player** on, unless you want to see both.
2. Check the connection. Host, ports and credentials are taken from the Kodi integration.

What happens:

- Better Kodi's media player is added to the **same device** as the Kodi media player, in the same area.
- The Kodi media player is **hidden**. It keeps working; Better Kodi uses it.
- **Assist**: if the Kodi media player was exposed to Assist, the exposure and its aliases move to Better Kodi's media player, so voice commands reach the right one. Check under **Settings → Voice assistants → Expose**; see [Exposing entities to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/).
- When you remove Better Kodi, the Kodi media player is shown again and gets its Assist settings back.

### Good to know

- Better Kodi starts even when Kodi (or the TV) is off, so you can switch it on through Better Kodi's media player.
- If Kodi's password changes, Home Assistant asks you to log in again (**Settings → Devices & services**).
- Both media players have the same name. Better Kodi's gets an entity ID ending in `_2` (for example `media_player.living_room_kodi_2`). Rename it under the entity's settings if you like.
- Point dashboards, automations and scripts to Better Kodi's media player. Your existing automations on the Kodi device's **turned on / turned off** triggers keep working, because Better Kodi forwards power commands to the Kodi media player.
- On the device page, Kodi is shown as the device's integration and Better Kodi's media player is listed among its entities. Since Home Assistant 2026.10 a device belongs to a single integration, so Better Kodi isn't listed there separately.

## Usage

### Play a link

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_kodi_2
data:
  media_content_type: url
  media_content_id: https://www.youtube.com/watch?v=dQw4w9WgXcQ
  enqueue: next  # play | next | add | replace
```

| `enqueue` | While Kodi plays from the queue | When nothing is playing |
| --- | --- | --- |
| `play` (default) | Plays now; the rest of the queue stays | Starts a new queue |
| `next` | Plays after the current video | Starts a new queue |
| `add` | Plays at the end of the queue | Starts a new queue |
| `replace` | Clears the queue and plays | Starts a new queue |

### Dashboard: paste a link, choose what happens

Create a text helper `input_text.kodi_link` (max length 255) and a script that reads it:

```yaml
script:
  kodi_link:
    alias: Play link on Kodi
    fields:
      enqueue:
        selector:
          select:
            options: [play, next, add, replace]
    sequence:
      - action: media_player.play_media
        target:
          entity_id: media_player.living_room_kodi_2
        data:
          media_content_type: url
          media_content_id: "{{ states('input_text.kodi_link') }}"
          enqueue: "{{ enqueue | default('play') }}"
```

Buttons on the dashboard then call `script.kodi_link` with `enqueue: next` and so on.

### Share from your Android phone

The Home Assistant Companion app for Android can share links to Home Assistant, which fires a `mobile_app.share` event:

```yaml
automation:
  - alias: Shared link to Kodi queue
    triggers:
      - trigger: event
        event_type: mobile_app.share
    actions:
      - action: media_player.play_media
        target:
          entity_id: media_player.living_room_kodi_2
        data:
          media_content_type: url
          media_content_id: "{{ trigger.event.data.url or trigger.event.data.text or trigger.event.data.subject }}"
          enqueue: add
```

Which field holds the link depends on the app you share from (`url` or `text`). If the shared text contains more than the link, Better Kodi picks out the first link.

### The queue

The media browser of Better Kodi's media player shows a **Queue** folder at the top, next to the Kodi library. The playing item is marked with ▶; select an item to jump to it. Library items (movies, episodes, music videos) can be queued from the media browser too.

The entity also has the attributes `queue_position` and `queue_length`.

### Services

| Service | What it does |
| --- | --- |
| `better_kodi.get_queue` | Returns the queue (position, title, file, type, current item). Use `response_variable`. |
| `better_kodi.remove_from_queue` | Removes the item at `position` (0-based). The playing item can't be removed. |
| `better_kodi.move_queue_item` | Moves the item at `position` to `to_position`. |
| `better_kodi.handover` | Continues playback on another Better Kodi media player (`destination`), at the same item and time (`resume`, default on). Stops here unless `keep_source_playing: true`. |
| `better_kodi.call_method` | Calls a Kodi JSON-RPC method with `params` and returns `result`. |
| `media_player.clear_playlist` | Clears the queue. |

Handover example:

```yaml
action: better_kodi.handover
target:
  entity_id: media_player.living_room_kodi_2
data:
  destination: media_player.bedroom_kodi_2
```

Handover uses file paths, so it also works when the two Kodis have separate libraries, as long as both can reach the files (for example on the same NAS) and have the same add-ons.

## Supported links

| Link | Needs in Kodi | Play now | Next / add |
| --- | --- | --- | --- |
| YouTube videos, Shorts, live, `youtu.be` | YouTube add-on (`plugin.video.youtube`) | ✓ | ✓ |
| YouTube playlists | YouTube add-on | ✓ | – |
| Rumble videos | nothing (resolved in Home Assistant) | ✓ | ✓ |
| Jellyfin web links (`…/web/#/details?id=…`) | Jellyfin for Kodi (`plugin.video.jellyfin`) | ✓ | ✓ |
| Jellyfin web links | JellyCon (`plugin.video.jellycon`) | ✓ | – |
| Direct media URLs (`.mp4`, `.mkv`, `.m3u8`, …), Kodi paths (`smb://`, `nfs://`, `plugin://`) | nothing | ✓ | ✓ |
| Media from Home Assistant's media browser | nothing | ✓ | ✓ |
| Other web pages | SendToKodi (`plugin.video.sendtokodi`) | ✓ | ✓ |

Better Kodi checks which add-ons are installed and enabled in Kodi. If a YouTube link needs the YouTube add-on and it's missing, SendToKodi is used when it is installed; otherwise you get an error that names the missing add-on. Jellyfin links always need one of the Jellyfin add-ons, because they need your Jellyfin login.

YouTube playlists and JellyCon links can only be played right away: their add-ons fill Kodi's queue themselves and clear it first. Jellyfin items you pick in Kodi's library are available in the media browser anyway.

Media from Home Assistant's media browser is passed to Kodi as a signed link that is valid for 24 hours. Queue entries of this kind stop working after that.

## How it works

See [docs/DESIGN.md](docs/DESIGN.md).

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

## License

[MIT](LICENSE). Better Kodi is an unofficial community project, not affiliated with the Kodi project or the Open Home Foundation. Kodi is a registered trademark of the XBMC Foundation.
