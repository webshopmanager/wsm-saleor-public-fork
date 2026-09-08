# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Fork-wide test setup: the storefront key every gated endpoint now demands.

`pytest-probe.sh` sources the box's env file, which does not carry a key, and an
unset key closes the endpoints by design. Setting it here rather than in the env
file keeps the fail-safe behaviour testable: a test that wants the closed case
overrides `settings.WSM_STOREFRONT_KEY` back to "".
"""

import pytest

from .tests import STOREFRONT_KEY


@pytest.fixture(autouse=True)
def wsm_storefront_key(settings):
    settings.WSM_STOREFRONT_KEY = STOREFRONT_KEY
    return STOREFRONT_KEY
