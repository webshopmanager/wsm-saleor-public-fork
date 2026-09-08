# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Test-support constants shared by every fork app's tests.

The storefront key lives here rather than in a conftest so a test module can
import it by name; `saleor/wsm/conftest.py` puts it into settings for the whole
fork test tree.
"""

STOREFRONT_KEY = "wsm-test-storefront-key"

# What the storefront server sends on the dealer endpoints, and on compose.
DEALER_HEADERS = {
    "HTTP_X_SALEOR_DOMAIN": "example.com",
    "HTTP_X_DEALER_PRICING_KEY": STOREFRONT_KEY,
}
COMPOSE_HEADERS = {
    "HTTP_X_SALEOR_DOMAIN": "bakeoff.test",
    "HTTP_X_CLIENT_ID": "storefront",
    "HTTP_X_COMPOSE_KEY": STOREFRONT_KEY,
}
