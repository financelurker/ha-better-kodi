# Based on tests/components/kodi from Home Assistant Core 2026.10.2
# (Apache-2.0). Modified for Better Kodi; see NOTICE and the git history.
"""Fixtures for Better Kodi tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return
