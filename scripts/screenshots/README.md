# README screenshots

The screenshots in `docs/images/` are taken on a real Home Assistant with a
simulated Kodi, so they can be redone after a change. All titles, covers and
links are made up (`fake_kodi_server.py`). Run every command from the
repository root.

1. Install Home Assistant in its own virtual environment, in the version the
   screenshots should show (currently 2026.10.2) with the frontend it pins
   (`homeassistant/components/frontend/manifest.json`), plus `pykodi` (for the
   Kodi integration) and `pillow`:

   ```bash
   python3.14 -m venv .ha && . .ha/bin/activate
   pip install homeassistant==2026.10.2 home-assistant-frontend==20260930.4 pykodi pillow
   ```

2. Install Playwright with Chromium (not part of the repository):

   ```bash
   npm install --no-save playwright && npx playwright install chromium
   ```

   If Playwright is installed elsewhere, point `PLAYWRIGHT_MODULE` to its
   `index.mjs` instead.

3. Start the simulated Kodi (HTTP on 8080, WebSocket on 9090; `-v` logs every
   WebSocket call):

   ```bash
   python scripts/screenshots/fake_kodi_server.py
   ```

   It draws its covers with DejaVu Sans Bold if installed, otherwise with
   Pillow's built-in font.

4. Start Home Assistant with a new, empty configuration folder that links this
   repository's integration:

   ```bash
   mkdir -p /tmp/ha/custom_components
   ln -s "$PWD/custom_components/better_kodi" /tmp/ha/custom_components/
   cat > /tmp/ha/configuration.yaml <<'EOF'
   homeassistant:
     name: Home
     unit_system: metric
     time_zone: Europe/Vienna
     country: AT
     currency: EUR
     internal_url: http://127.0.0.1:8123
   frontend:
   config:
   http:
   media_source:
   EOF
   hass -c /tmp/ha
   ```

5. Once Home Assistant answers, onboard it and set everything up: user `demo`,
   the Kodi integration, Better Kodi, the area and a dashboard. This only works
   on a Home Assistant that hasn't been onboarded yet (start again with an empty
   folder otherwise). It prints JSON with the device ID (and an access token):

   ```bash
   DEVICE_ID=$(python scripts/screenshots/setup_ha.py http://127.0.0.1:8123 | jq -r .device_id)
   ```

6. Take the screenshots:

   ```bash
   DEVICE_ID=$DEVICE_ID node scripts/screenshots/shoot.mjs http://127.0.0.1:8123 docs/images
   ```

   Add a name (for example `queue`) to take only that one.

7. Update the image URLs in the README: `python scripts/readme_images.py`.
