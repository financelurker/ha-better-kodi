# Better Kodi

Enhanced Kodi integration for Home Assistant: play, play next or enqueue URLs (YouTube & more) like Cast Kodi, with queue management in the media browser.

Better Kodi does not replace the built-in Kodi integration. It sits on top of it, like Better Thermostat does for thermostats: you pick a Kodi media player, and Better Kodi gives you a media player that does everything the original does, plus:

- **Play links** from YouTube, Jellyfin and direct media URLs: play now, play next, or add to the end of the queue (`enqueue: play | next | add | replace`)
- **The queue in the media browser**, updated live: see what's coming, jump to an item
- **Queue services**: read the queue, remove and move items
- **Handover**: continue the current video, with its whole queue and at the same position, on another Kodi
- **Switch Kodi on and off** by itself: Wake on LAN, suspend, hibernate, shut down. No scripts or automations needed
- **`call_method` with a response**: call any Kodi JSON-RPC method and use the result in your script

> Kodi's **music** queue is left alone for now; Better Kodi only works with the video queue. For music, see [Better Media Player](https://github.com/financelurker/ha-better-media-player) and Music Assistant. If you use Kodi for music and know what a music queue in Better Kodi should do, input is always welcome: [open an issue](https://github.com/financelurker/ha-better-kodi/issues).

## Screenshots

<table>
<tr>
<td width="50%" valign="top"><b>Paste a link</b> – play it now, next, at the end of the queue, or replace the queue<br><br><img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/dashboard.png?v=0a0aa34eee" alt="Dashboard with Kodi's media card, a link field and four buttons"></td>
<td width="50%" valign="top"><b>Media browser</b> – a movie or episode offers play now, play next, add to queue and replace queue<br><br><img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/choices.png?v=25357c890e" alt="Media browser: a movie with Play now, Play next, Add to queue and Replace queue"></td>
</tr>
<tr>
<td width="50%" valign="top"><b>The queue</b> – a folder at the top of Kodi's media browser<br><br><img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/media-browser.png?v=c60a3f2509" alt="Media browser: the Queue folder next to Kodi's library"></td>
<td width="50%" valign="top"><b>What's coming</b> – the playing entry marked, a click jumps to an entry<br><br><img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/queue.png?v=ed7f7ec221" alt="The queue with the playing entry marked"></td>
</tr>
</table>

Taken on Home Assistant 2026.10.2, with a simulated Kodi and made-up titles.

## Requirements

- Home Assistant 2026.10 or newer
- The built-in [Kodi integration](https://www.home-assistant.io/integrations/kodi/), set up for each Kodi
- In Kodi, under **Settings → Services → Control**:
  - **Allow remote control via HTTP** (already on if the Kodi integration works)
  - **Allow remote control from applications on other systems**, for live queue updates
- The Kodi add-ons for the sites you want to use (see [Supported links](#supported-links))

## Installation

### HACS

[![Open your Home Assistant instance and open Better Kodi in the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=financelurker&repository=ha-better-kodi&category=integration)

1. Click the button above. It opens Better Kodi in HACS on your Home Assistant and adds the repository if needed.
2. Select **Download** and restart Home Assistant.

Or by hand: in HACS, open the menu → **Custom repositories**, add `https://github.com/financelurker/ha-better-kodi` with category **Integration**, then download **Better Kodi** and restart.

### Manual

Copy `custom_components/better_kodi` into the `custom_components` folder of your Home Assistant configuration and restart.

## Setup

[![Open your Home Assistant instance and start setting up Better Kodi.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=better_kodi)

Or **Settings → Devices & services → Add integration → Better Kodi**. Set it up once per Kodi.

1. Choose the Kodi media player. Keep **Hide the Kodi media player** on, unless you want to see both.
2. Check the connection. Host, ports and credentials are taken from the Kodi integration.

What happens:

- Better Kodi's media player is added to the **same device** as the Kodi media player, in the same area.
- The Kodi media player is **hidden**. It keeps working; Better Kodi uses it.
- **Assist**: if the Kodi media player was exposed to Assist, the exposure and its aliases move to Better Kodi's media player, so voice commands reach the right one. Check under **Settings → Voice assistants → Expose**; see [Exposing entities to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/).
- When you remove Better Kodi, the Kodi media player is shown again and gets its Assist settings back.

<img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/device.png?v=82a75c57ff" alt="Kodi device page: Better Kodi's media player, link field and buttons, notifier and library buttons" width="520">

The hidden Kodi media player and Better Kodi\'s media player share the device.

### Options

Under **Settings → Devices & services → Better Kodi → Configure**:

<details>
<summary>Screenshot of the options</summary>
<br>
<img src="https://raw.githubusercontent.com/financelurker/ha-better-kodi/main/docs/images/options.png?v=6459f6e0bf" alt="Better Kodi options: switching Kodi on and off, links shared from phones, extras" width="440">
</details>

| Option | Default | What it does |
| --- | --- | --- |
| Switch Kodi on with | Kodi automation | **Wake on LAN**: Better Kodi sends the packet itself. **Kodi automation**: runs your "Kodi turned on" automation of the Kodi device, as the Kodi integration does. |
| MAC address of Kodi | read from Kodi | Needed for Wake on LAN. Better Kodi reads it from Kodi when you open the options while Kodi is on. |
| Broadcast address | `255.255.255.255` | Where the Wake on LAN packet goes. Use the broadcast address of Kodi's network (such as `192.168.1.255`) if the default doesn't reach Kodi. |
| Switch Kodi off with | Kodi automation | **Suspend**, **Hibernate**, **Shut down** or **Quit Kodi** (Kodi commands), or your "Kodi turned off" automation. Quit Kodi leaves the device on (see below). |
| Wake Kodi before sending | on | See below. |
| Wait at most | 120 s | How long to wait for Kodi to wake up. |
| Links shared from phones | Add to queue, all phones | See [Share from your phone](#share-from-your-phone). |
| Choices in the media browser | on | See [Play next or add to the queue from the media browser](#play-next-or-add-to-the-queue-from-the-media-browser). |
| Link field and buttons | on | See [Paste a link](#paste-a-link-link-field-and-buttons). Off removes them. |
| Library and restart buttons | on | See [Messages on the TV, library and restart](#messages-on-the-tv-library-and-restart). Off removes them. |
| Messages on Kodi | on | The notify entity, see the same section. Off removes it. |
| Look up titles online | on | Asks YouTube for titles and images of YouTube entries. Off: Home Assistant doesn't contact YouTube for Better Kodi. |

### Switching Kodi on and off

Better Kodi can switch Kodi on and off itself, so the usual Wake-on-LAN and suspend scripts plus the "Kodi turned on / off" automations are no longer needed:

1. Open Better Kodi's options **while Kodi is on**. The MAC address is filled in from Kodi.
2. Set **Switch Kodi on with** to **Wake on LAN**, and **Switch Kodi off with** to, for example, **Suspend**.
3. Delete your old turn-on/turn-off automations and scripts. Better Kodi doesn't run them anymore for the directions you switched away from "Kodi automation" (only commands sent directly to the hidden Kodi media player still would).

Needed on the Kodi device: Wake on LAN enabled (BIOS/UEFI and network adapter, often "Wake on Magic Packet"), and permission for Kodi to suspend, hibernate or shut down. Kodi reports what it may do; if it may not, Better Kodi says so instead of failing silently. Wake on LAN packets are broadcasts, so Home Assistant has to be in the same network as Kodi (with Home Assistant in Docker: host networking).

Good to know:

- The MAC address read from Kodi is that of the network connection Kodi uses right now. If that's Wi-Fi, check it: Wake on LAN usually only works over cable.
- **Quit Kodi** only closes the Kodi app; the device stays on. Wake on LAN can't start Kodi again afterwards, so don't combine it with waking before sending.

### Kodi suspended or off: woken up automatically

If the Kodi media player is **off** when you send something (a link, a media browser item, a button, a handover to that Kodi), Better Kodi:

1. switches it on, with Wake on LAN or your "Kodi turned on" automation (see above);
2. waits until the Kodi media player is no longer off (and up to 30 seconds more until Kodi answers). Wake on LAN packets are repeated every 10 seconds meanwhile, in case one gets lost;
3. then sends.

If Kodi is on, everything is sent right away. Several links sent while Kodi wakes share one wake-up, so an automation runs only once. With the automation setting and no "Kodi turned on" automation you get an error right away. If Kodi isn't awake within the time limit, you get an error and the link stays in the Link field.

### Good to know

- Better Kodi starts even when Kodi (or the TV) is off, so you can switch it on through Better Kodi's media player.
- If Kodi's password changes, Home Assistant asks you to log in again (**Settings → Devices & services**).
- Both media players have the same name. Better Kodi's gets an entity ID ending in `_2` (for example `media_player.living_room_kodi_2`). Rename it under the entity's settings if you like.
- Point dashboards, automations and scripts to Better Kodi's media player. With the default power settings, your existing automations on the Kodi device's **turned on / turned off** triggers keep working, because Better Kodi forwards power commands to the Kodi media player.
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

### Paste a link: link field and buttons

Better Kodi adds a **Link** field and four buttons to the Kodi device: **Play link now**, **Play link next**, **Add link to queue** and **Replace queue with link**. Paste a link, press a button, done; the field is cleared afterwards (and kept if the link can't be played).

Put them on a dashboard, for example (the card in the screenshot above):

```yaml
type: entities
title: Send a link to Kodi
entities:
  - entity: text.living_room_kodi_link
    name: Link
  - type: buttons
    entities:
      - entity: button.living_room_kodi_play_link_now
        name: Play now
      - entity: button.living_room_kodi_play_link_next
        name: Play next
      - entity: button.living_room_kodi_add_link_to_queue
        name: Add to queue
      - entity: button.living_room_kodi_replace_queue_with_link
        name: Replace queue
```

The field holds up to 255 characters, the longest value Home Assistant can store. For longer links use `media_player.play_media` directly.

Don't need them? Switch off **Link field and buttons** in the options.

### Play next or add to the queue from the media browser

Home Assistant's media browser itself has no "play next" or "add to queue" option. Better Kodi adds one:

- **Click** a movie, episode, music video or media source item (for example from Jellyfin): a small folder opens with **▶ Play now**, **⏭ Play next**, **➕ Add to queue** and **⟲ Replace queue**.
- The **play button** on the item still plays it right away.

Folders such as shows and seasons, and items that can't be queued (TV channels), behave as before.

Good to know: media picked this way in the media selector of an automation or script stores a Better Kodi ID (`better_kodi://…`), which only Better Kodi's media player understands. Dashboard cards that browse media through Better Kodi's media player see the same choices.

Prefer the media browser as with other players (a click plays right away)? Switch off **Choices in the media browser** in the options.

### Messages on the TV, library and restart

Better Kodi adds to the Kodi device, without any YAML or scripts:

- **A notify entity** named like the device (for example `notify.living_room_kodi`). Kodi shows the message for 8 seconds on top of whatever plays:

  ```yaml
  action: notify.send_message
  target:
    entity_id: notify.living_room_kodi
  data:
    title: Front door
    message: Someone rang the bell
  ```

  `notify.send_message` takes a title and a message; without a title, the device name is the title. If Kodi is off, the action fails with an error (add `continue_on_error: true` to the step if the automation should go on anyway).
- **Update video library**, **Clean video library** (`VideoLibrary.Scan`, `VideoLibrary.Clean`) and **Restart device** (`System.Reboot`, only if Kodi may restart the device) as buttons, under the device's configuration entities.

Switching the TV on and off with Kodi: Kodi does this itself over HDMI-CEC (Kodi: **Settings → System → Input → Peripherals → CEC adapter**, "Devices to power on/off" when Kodi wakes up and goes to sleep). Combined with Better Kodi's Wake on LAN and Suspend, no CEC add-on or script is needed.

### Share from your phone

Share a link from any app on your Android phone (YouTube, Brave, Chrome, …) with **Home Assistant**, and Kodi plays it. No automation needed.

- With one Kodi, the link goes straight to it (added to the queue by default; if nothing is playing, it starts playing).
- With several Kodis, your phone shows a notification **"Which Kodi should play …?"** with one button per Kodi (the one that is playing first). Tap one, and the link goes there. The question is valid for 10 minutes; Android shows at most three buttons.
- If the link can't be played (for example the YouTube add-on is missing, or Kodi doesn't wake up), the phone gets a notification with the reason.
- If the shared text contains more than the link, Better Kodi picks out the first link. Shares without a link are ignored.

Per Kodi, in the options under **Links shared from phones**: what a shared link does (**Play now**, **Play next**, **Add to queue**, **Replace queue**, or **Off**), **Only from these phones** (for example the bedroom Kodi only for the tablet in the bedroom; a phone listed for Kodis sends only to those, and without asking if it's just one), and whether errors are reported on the phone.

Asking and error notifications need the phone's notifications from the Home Assistant app (`notify.mobile_app_…`). If several Kodis could take the link but the phone can't be asked (no notifications, or Home Assistant doesn't know the phone), the link goes to the Kodi that is playing, if exactly one is; otherwise nothing is played. YouTube playlists can only be played right away: set **Play now** if you share playlists, otherwise the phone reports that they can't be queued. If you had built a share automation yourself (as in earlier versions of this README), delete it, or links play twice.

### The queue

The media browser of Better Kodi's media player shows a **Queue** folder at the top, next to the Kodi library. The playing item is marked with ▶; select an item to jump to it.

Entries get their titles even when another app (Cast Kodi, Yatse) queued them: Kodi only knows unplayed add-on items by their path ("play"), so Better Kodi looks up the titles of YouTube entries (unless **Look up titles online** is off) and of Jellyfin stream links itself. Entries that Kodi or an add-on already named keep their name.

The entity also has the attributes `queue_position` and `queue_length`.

### Media from Home Assistant's media browser: media-source backends

Integrations such as Jellyfin hand players a plain stream link. Kodi (and so Cast Kodi) names it "stream", and playback isn't reported back to the media server. A **media-source backend** teaches Better Kodi about the items of one such integration. So far there is one, for Jellyfin; for other integrations the stream link is queued as is.

#### Jellyfin

This also applies to Jellyfin links you share, paste or send yourself: **Copy stream URL** in the Jellyfin app (`…/Items/<id>/Download?api_key=…`), stream and HLS links (`…/Videos/<id>/stream`, `…/master.m3u8`). Better Kodi reads the item from Jellyfin and queues, in this order:

1. **The item from Kodi's own library**, if the Jellyfin for Kodi add-on synced it. Then Kodi knows title and artwork itself, and every app shows them.
2. **Jellyfin for Kodi's play path**, if the add-on is installed but the item isn't synced. Playback is then reported to Jellyfin (watched state, resume).
3. **The stream link** of the item. A "Download" link is turned into the item's stream link, which Kodi can play (Kodi often can't play "Download" links).

Better Kodi's own queue shows the title in every case. In the 2nd and 3rd case Kodi itself only learns the title once the item plays, so other apps show it as "stream" or "Download" until then.

Title, cover and the direct-path match need Home Assistant to reach the Jellyfin server named in the link (a `jellyfin.local` name may not resolve inside Docker). If it can't, Better Kodi still finds the item in Kodi's library by its ID, or uses Jellyfin for Kodi's play path. Links with folders (a show, a season) are passed on unchanged. API keys in links are never written to the log.

#### Adding a backend (Plex, Emby, …)

Backends are an extension point; contributions are welcome. A backend is a class in `custom_components/better_kodi/backends/` that implements `MediaSourceBackend` from [`backends/base.py`](custom_components/better_kodi/backends/base.py):

| Method | Does |
| --- | --- |
| `recognizes(url)` | Says, without network calls, whether a stream link belongs to this media server. |
| `async_kodi_item(context, url)` | Returns the Kodi item to queue: a Kodi library item (`{"movieid": 7}`), a Kodi add-on path, or the link, plus title and image for the queue. `None`: a media browser item is queued as is, a link goes to the link resolvers. |
| `async_title(context, file)` | Optional: title and image of a queue entry with such a link, for entries other apps queued. |

`context` gives the backend a web session, the Kodi connection and the list of installed Kodi add-ons. List an instance in `BACKENDS` in `backends/__init__.py`, and add tests (`tests/test_backend_jellyfin.py` shows how). A failing backend never breaks playing: Better Kodi then carries on as if it returned `None`.

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
| Jellyfin web links (`…/web/#/details?id=…`) | Jellyfin for Kodi (`plugin.video.jellyfin`) | ✓ | ✓ |
| Jellyfin web links | JellyCon (`plugin.video.jellycon`) | ✓ | – |
| Jellyfin stream links ("Copy stream URL": `…/Items/<id>/Download?api_key=…`, `…/Videos/<id>/stream…`) | best with Jellyfin for Kodi (see [Jellyfin](#jellyfin)) | ✓ | ✓ |
| Direct media URLs (`.mp4`, `.mkv`, `.m3u8`, …), Kodi paths (`smb://`, `nfs://`, `plugin://`) | nothing | ✓ | ✓ |
| Media from Home Assistant's media browser | nothing | ✓ | ✓ |
| Other web pages | SendToKodi (`plugin.video.sendtokodi`) | ✓ | ✓ |

Rumble isn't supported yet (an attempt didn't produce usable streams). Rumble links go to SendToKodi if it is installed; otherwise use Cast Kodi for them.

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
