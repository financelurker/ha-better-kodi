# Contributing

Thanks for helping out! Bug reports and pull requests are welcome.

- Read [docs/DESIGN.md](docs/DESIGN.md) first and update it when behaviour changes.
- Add or update tests for every change; `tests/fake_kodi.py` simulates Kodi's playlist and player.
- Run `ruff check .`, `ruff format --check .` and `pytest` before opening a pull request.
- Don't copy code from other projects. Facts such as URL formats are fine; mention the source.
- New link resolvers go into `resolvers.py`, with a row in the README and DESIGN tables.
- Screenshots: see [scripts/screenshots/README.md](scripts/screenshots/README.md). After adding or changing an image in `docs/images/`, run `python scripts/readme_images.py` (CI checks it): README images use absolute, versioned URLs so that HACS shows them and no cache shows an old version.
- Support for another media server in Home Assistant's media browser (Plex, Emby, …) is a media-source backend in `backends/`; see the README.

Versions: the GitHub release tag is the only source of the version (see "Releases" in the README). `manifest.json` keeps the placeholder `0.0.0`; don't change it, and don't write the version anywhere else. Code that needs the version reads it from the manifest (`homeassistant.loader.async_get_integration(hass, DOMAIN).version`).

By contributing you agree that your contribution is licensed under the MIT License.
