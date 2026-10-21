# Contributing

Thanks for helping out! Bug reports and pull requests are welcome.

- Read [docs/DESIGN.md](docs/DESIGN.md) first and update it when behaviour changes.
- Add or update tests for every change; `tests/fake_kodi.py` simulates Kodi's playlist and player.
- Run `ruff check .`, `ruff format --check .` and `pytest` before opening a pull request.
- Don't copy code from other projects. Facts such as URL formats are fine; mention the source.
- New link resolvers go into `resolvers.py`, with a row in the README and DESIGN tables.
- Screenshots: see [scripts/screenshots/README.md](scripts/screenshots/README.md). After adding or changing an image in `docs/images/`, run `python scripts/readme_images.py` (CI checks it): README images use absolute, versioned URLs so that HACS shows them and no cache shows an old version.
- Brand icon: `custom_components/better_kodi/brand/` (`icon.png` 256 px, `icon@2x.png` 512 px, and `dark_` variants for dark mode, transparent background). The SVG sources are in `docs/brand/`; export them with `node scripts/brand/render.mjs` (Playwright).
- Support for another media server in Home Assistant's media browser (Plex, Emby, …) is a media-source backend in `backends/`; see the README.

By contributing you agree that your contribution is licensed under the MIT License.

## Releases and versions

The GitHub release tag is the only source of the version. `manifest.json` keeps the placeholder `0.0.0` (Home Assistant needs a version to load the integration); don't change it, and don't write the version anywhere else. Code that needs the version reads it from the manifest (`homeassistant.loader.async_get_integration(hass, DOMAIN).version`).

1. Create a release on GitHub with a tag such as `v1.2.3`, on a commit that contains the *Release* workflow. Pre-releases get their ZIP the same way; HACS offers them only with **Show beta versions** turned on.
2. The *Release* workflow takes the version from the tag (without the leading `v`; it stops with an error if the tag isn't a version number), writes it into `manifest.json`, packs the integration as `better_kodi.zip` and attaches it to the release.
3. HACS installs that ZIP (`zip_release` in `hacs.json`), so Home Assistant shows the release version.

To rebuild the ZIP of an existing release, run the *Release* workflow under **Actions → Release → Run workflow** with the release's tag.

Good to know:

- HACS installs **only from releases** (their ZIP). Installing the latest commit of the default branch doesn't work, and as long as there is no full release (only pre-releases or none), HACS installs fail.
- The ZIP is attached about a minute after the release is published. An update started in that minute fails; just try again.
- The workflow stops if the tag isn't a version Home Assistant accepts (it checks with the same rules as Home Assistant), so a release can't ship an integration that Home Assistant refuses to load.
