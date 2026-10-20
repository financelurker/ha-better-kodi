"""Tests for keeping secrets out of logs."""

import pytest

from custom_components.better_kodi.redact import redact_url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "http://j:8096/Items/1/Download?api_key=SECRET",
            "http://j:8096/Items/1/Download?api_key=***",
        ),
        ("http://j/x?static=true&ApiKey=SECRET&a=1", "http://j/x?static=true&ApiKey=***&a=1"),
        (
            "Could not play http://kodi:pw@host/a.mp4: no",
            "Could not play http://***@host/a.mp4: no",
        ),
        ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
        ("", ""),
    ],
)
def test_redact_url(text: str, expected: str) -> None:
    assert redact_url(text) == expected
