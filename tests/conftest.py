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
