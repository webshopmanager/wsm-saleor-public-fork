# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every gated endpoint refuses a caller that cannot present the tenant key.

The happy path is not repeated here: the per-app suites all post the real key
through `DEALER_HEADERS` / `COMPOSE_HEADERS` and would go red the moment the
decorator refused a legitimate storefront call. What this file owns is the
refusal, and the fail-safe: no key configured closes the door, never opens it.
"""

import json

import pytest

from . import STOREFRONT_KEY

pytestmark = pytest.mark.django_db

# Every route the decorator is on. Endpoint 1 (option-sets) is public catalog
# data and is deliberately absent.
GATED = (
    "/wsm/dealer_pricing/api/storefront/prices",
    "/wsm/dealer_pricing/api/checkout/dealer-line",
    "/wsm/dealer_pricing/api/checkout/dealer-line/reprice",
    "/wsm/compose/api/checkout/configured-line",
    "/wsm/containers/api/checkout/kit-line",
)


def post(client, url, **headers):
    return client.post(
        url, data=json.dumps({}), content_type="application/json", **headers
    )


@pytest.mark.parametrize("url", GATED)
def test_no_key_is_refused(client, url):
    assert post(client, url).status_code == 401


@pytest.mark.parametrize("url", GATED)
def test_wrong_key_is_refused(client, url):
    for header in ("HTTP_X_DEALER_PRICING_KEY", "HTTP_X_COMPOSE_KEY"):
        response = post(client, url, **{header: STOREFRONT_KEY + "x"})
        assert response.status_code == 401, (url, header)


@pytest.mark.parametrize("url", GATED)
def test_unset_key_closes_the_endpoint(client, url, settings):
    """Fail SAFE. An unconfigured tenant sells nothing here, rather than everything."""
    settings.WSM_STOREFRONT_KEY = ""
    response = post(client, url, HTTP_X_DEALER_PRICING_KEY="")
    assert response.status_code == 401
    assert post(client, url, HTTP_X_COMPOSE_KEY=STOREFRONT_KEY).status_code == 401


def test_either_header_spelling_satisfies_either_app(client):
    """The header name is the caller's habit; the secret is the identity."""
    for header in ("HTTP_X_DEALER_PRICING_KEY", "HTTP_X_COMPOSE_KEY"):
        for url in GATED:
            response = post(client, url, **{header: STOREFRONT_KEY})
            assert response.status_code != 401, (url, header)


def test_public_option_sets_endpoint_stays_open(client):
    response = client.get(
        "/wsm/compose/api/storefront/products/saleor/UHJvZHVjdDo5OTk5OTk=/option-sets"
    )
    assert response.status_code == 404  # unknown product, not unauthorized
