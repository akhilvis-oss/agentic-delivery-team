"""Clean up upgrade timers without loading lifecycle into unrelated tests."""

import sys

import pytest


@pytest.fixture(autouse=True)
def cancel_upgrade_timer():
    yield
    upgrade = sys.modules.get('lifecycle.upgrade')
    timer = getattr(upgrade, '_pending_timer', None)
    if timer is not None:
        timer.cancel()
        if timer.ident is not None:
            timer.join(timeout=2)
        upgrade._pending_timer = None


@pytest.fixture(autouse=True)
def clear_app_token_cache():
    tokens = sys.modules.get("lifecycle.app_tokens")
    if tokens is not None:
        tokens.clear_cache()
    yield
    tokens = sys.modules.get("lifecycle.app_tokens")
    if tokens is not None:
        tokens.clear_cache()
