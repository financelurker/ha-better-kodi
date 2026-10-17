# Better Kodi

Enhanced Kodi integration for Home Assistant: play, play next or enqueue URLs (YouTube & more) like Cast Kodi, with queue management in the media browser.

> **Status: early development.** The current version is a port of the Kodi integration from Home Assistant Core 2026.10.2 under its own domain (`better_kodi`). The queue features below are being built now.

## Why

The built-in Kodi integration can start playback, but it cannot add something to the queue or play it next. `media_player.play_media` ignores `enqueue`, and `kodi.call_method` only reports its result as an event, so scripts can't easily read the current queue position.

Better Kodi adds what browser extensions like Cast Kodi can do, directly in Home Assistant.

## Planned features

- `media_player.play_media` with `enqueue: play | next | add | replace`
- Paste a page URL: YouTube videos, Shorts and playlists are turned into the right Kodi add-on path automatically; direct media URLs are passed through. Only sites whose Kodi add-on is installed are enabled.
- The current Kodi queue as a folder in the media browser, kept up to date live over WebSocket
- Services to read the queue, remove and move items
- `better_kodi.call_method` that returns Kodi's response to the calling script (`response_variable`)

The general parts (enqueue, queue services, method responses) are meant to be proposed to Home Assistant Core once they have proven themselves here. Site-specific parts such as URL handling stay in Better Kodi.

## Installation

### HACS

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/financelurker/ha-better-kodi` with category **Integration**.
3. Install **Better Kodi** and restart Home Assistant.

### Manual

Copy `custom_components/better_kodi` into the `custom_components` folder of your Home Assistant configuration and restart.

### Setup

**Settings → Devices & services → Add integration → Better Kodi.** Kodi instances on your network are also discovered automatically.

In Kodi, enable **Allow control of Kodi via HTTP**, and for live updates also **Allow remote control from applications on other systems** (System → Settings → Services → Control).

Requires Home Assistant 2026.10 or newer.

## Using it alongside the built-in Kodi integration

Better Kodi runs independently of the built-in Kodi integration. You can keep both, but each creates its own media player for the same Kodi, so you will usually want to use one of them per Kodi instance.

Differences to the built-in integration:

| Built-in Kodi | Better Kodi |
| --- | --- |
| `kodi.call_method` | `better_kodi.call_method` |
| `kodi.add_to_playlist` | `better_kodi.add_to_playlist` |
| Event `kodi_call_method_result` | Event `better_kodi_call_method_result` |
| Event `kodi_keypress` | Event `better_kodi_keypress` |
| Notification platform (`notify`) | Not included; keep using the built-in one if you need it |

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

## License and credits

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Based on the [Kodi integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/kodi) from Home Assistant Core. This is an unofficial community project, not affiliated with the Kodi project or the Open Home Foundation. Kodi is a registered trademark of the XBMC Foundation.
