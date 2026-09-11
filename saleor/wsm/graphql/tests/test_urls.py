# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""`saleor.wsm.urls` is the root urlconf, so it owes both halves of the map."""

import pytest
from django.urls import resolve
from django.urls.exceptions import Resolver404

from .. import schema as wsm_schema

FORK_ROUTES = [
    ("/wsm/compose/api/checkout/configured-line", "wsm-compose-configured-line"),
    (
        "/wsm/compose/api/storefront/products/saleor/UHJvZHVjdDox/option-sets",
        "wsm-compose-option-sets",
    ),
    ("/wsm/dealer_pricing/api/storefront/prices", "wsm-dealer-prices"),
    ("/wsm/dealer_pricing/api/checkout/dealer-line", "wsm-dealer-line"),
    ("/wsm/dealer_pricing/api/checkout/dealer-line/reprice", "wsm-dealer-line-reprice"),
    ("/wsm/containers/api/checkout/kit-line", "wsm-containers-kit-line"),
]

CORE_ROUTES = [
    ("/.well-known/jwks.json", "jwks"),
    ("/thumbnail/UHJvZHVjdDox/128/", "thumbnail"),
    ("/plugins/global/mirumee.notifications/", "plugins-global"),
]


@pytest.mark.parametrize(("path", "name"), FORK_ROUTES)
def test_the_fork_rest_routes_resolve_exactly_as_before(path, name):
    assert resolve(path).url_name == name


@pytest.mark.parametrize(("path", "name"), CORE_ROUTES)
def test_core_routes_still_resolve_through_the_included_saleor_urls(path, name):
    """The half a root-urlconf swap can silently drop.

    `saleor.urls` is now INCLUDED rather than including us, so forgetting that
    line would leave every core URL a 404 while every fork test still passed.
    """
    assert resolve(path).url_name == name


@pytest.mark.parametrize("path", ["/admin/", "/admin/login/"])
def test_the_django_admin_is_gone(path):
    """A 200 on a deleted admin is the false green this whole unit is about.

    The merchant UI is native Saleor Dashboard screens over
    `saleor/wsm/graphql` (ruling Dana 2026-09-10), so the second login page on
    the API host must not resolve at all. `saleor.urls` is included below us
    and never carried an admin, so a pass here is the whole map answering.
    """
    with pytest.raises(Resolver404):
        resolve(path)


def test_graphql_is_served_by_the_composed_schema_not_stock():
    """Ours is listed first, so ours is what `/graphql/` resolves to."""
    view = resolve("/graphql/").func

    assert view.view_initkwargs["schema"] is wsm_schema.schema
