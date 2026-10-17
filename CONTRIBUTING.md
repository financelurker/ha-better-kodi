# Contributing

Thanks for helping out!

## License of contributions

By submitting a pull request you agree that your contribution is licensed under the Apache License 2.0, like the rest of this project, and that it may later be proposed upstream to [Home Assistant Core](https://github.com/home-assistant/core) (which is also Apache-2.0). Upstream submissions require signing the Home Assistant Contributor License Agreement; if your code is part of such a submission, you will be asked before it is sent or invited to submit it yourself.

## Keeping the upstream path open

Better Kodi is derived from the Kodi integration in Home Assistant Core. To keep changes easy to send back:

- Follow Home Assistant's code style and [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) rules.
- Keep general features (enqueue, queue handling, service responses) separate from site-specific ones (URL resolvers), so the general part can go upstream on its own.
- Add or update tests for every change.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt ruff
ruff check . && ruff format --check .
pytest
```
