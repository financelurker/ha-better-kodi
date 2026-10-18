# Contributing

Thanks for helping out! Bug reports and pull requests are welcome.

- Read [docs/DESIGN.md](docs/DESIGN.md) first and update it when behaviour changes.
- Add or update tests for every change; `tests/fake_kodi.py` simulates Kodi's playlist and player.
- Run `ruff check .`, `ruff format --check .` and `pytest` before opening a pull request.
- Don't copy code from other projects. Facts such as URL formats are fine; mention the source.
- New link resolvers go into `resolvers.py`, with a row in the README and DESIGN tables.

Versions: `manifest.json` keeps the placeholder `0.0.0`; Home Assistant needs the key, but the real version comes from git (HACS shows the commit) and later from GitHub release tags. Don't bump it by hand.

By contributing you agree that your contribution is licensed under the MIT License.
