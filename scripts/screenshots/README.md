# README screenshots

The screenshots in `docs/images/` are taken on a real Home Assistant with a
simulated Kodi, so they can be redone after a change. All titles and covers are
made up (`fake_kodi_server.py`).

1. Install Home Assistant in its own virtual environment, in the version the
   screenshots should show (currently 2026.10.2), with its frontend, plus
   `pykodi` (for the Kodi integration) and `pillow`:

   ```bash
   python3.14 -m venv .ha && . .ha/bin/activate
   pip install homeassistant==2026.10.2 home-assistant-frontend==20260930.4 pykodi pillow
   ```

2. Start the simulated Kodi (HTTP on 8080, WebSocket on 9090):

   ```bash
   python scripts/screenshots/fake_kodi_server.py
   ```

3. Start Home Assistant with an empty configuration folder that links this
   repository's integration, for example:

   ```bash
   mkdir -p /tmp/ha/custom_components
   ln -s "$PWD/custom_components/better_kodi" /tmp/ha/custom_components/
   printf 'frontend:\nconfig:\nhttp:\nmedia_source:\n' > /tmp/ha/configuration.yaml
   hass -c /tmp/ha
   ```

4. Onboard and set everything up (user `demo`, Kodi and Better Kodi, area, a
   dashboard). It prints the device ID:

   ```bash
   python scripts/screenshots/setup_ha.py http://127.0.0.1:8123
   ```

5. Take the screenshots with Playwright (Chromium):

   ```bash
   DEVICE_ID=<device id> node scripts/screenshots/shoot.mjs http://127.0.0.1:8123 docs/images
   ```

   Add a name (for example `queue`) to take only one.

6. Update the image URLs in the README: `python scripts/readme_images.py`.
