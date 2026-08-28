# WSM-FORK: fork-owned file. See FORK-NOTES.md.
import datetime
import logging
from contextlib import contextmanager

import graphene
import pytest
from django.conf import settings
from django.db import (
    OperationalError,
    ProgrammingError,
    connection,
    connections,
    transaction,
)
from django.db.models import Count, Min
from django.test.utils import CaptureQueriesContext

from ...attribute.utils import associate_attribute_values_to_instance
from ...core.db.connection import allow_writer
from ...graphql.tests.utils import get_graphql_content
from ...product.models import (
    Category,
    Collection,
    CollectionProduct,
    Product,
    ProductChannelListing,
    ProductVariant,
    ProductVariantChannelListing,
)
from .. import models as wsm_models
from ..models import (
    SECONDARY_CATEGORIES_TABLE,
    clear_secondary_categories_cache,
    secondary_categories_available,
)

QUERY_CATEGORY_PRODUCTS = """
    query ($id: ID!, $channel: String) {
        category(id: $id) {
            products(first: 20, channel: $channel) {
                totalCount
                edges {
                    node {
                        id
                    }
                }
            }
        }
    }
"""


@pytest.fixture(autouse=True)
def clear_table_probe_cache():
    clear_secondary_categories_cache()
    yield
    clear_secondary_categories_cache()


@pytest.fixture(autouse=True)
def probe_every_time(monkeypatch):
    """Re-probe on every call, so a test's DDL is never masked by the TTL cache.

    Tests that need the shipped cache instead request `real_probe_ttl`, which
    overrides this. A cold review found that pinning the TTL to zero for the
    whole module meant the one thing never tested was the value production runs.
    """
    monkeypatch.setattr(wsm_models, "PROBE_TTL_SECONDS", 0)


@pytest.fixture
def real_probe_ttl(probe_every_time, monkeypatch):
    """Run with the TTL production runs, cache and all.

    Depends on `probe_every_time` so that it is always applied after it, whatever
    order pytest resolves the two in.
    """
    monkeypatch.setattr(wsm_models, "PROBE_TTL_SECONDS", 60)
    clear_secondary_categories_cache()


# Byte-for-byte the DDL PartsLogic's Go migrator emits (migration.go:801-891):
# both single-column indexes, the uniqueness constraint and both foreign keys
# with ON DELETE CASCADE. The fixture has to carry all of it, because the perf
# argument for this feature depends on the leading-`category_id` index and the
# read side's tolerance of deleted products depends on the cascade.
SECONDARY_CATEGORIES_DDL = """
    CREATE TABLE product_secondary_categories (
        id bigserial PRIMARY KEY,
        product_id bigint NOT NULL,
        category_id bigint NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_product_secondary_category
            UNIQUE (product_id, category_id),
        CONSTRAINT fk_psc_product FOREIGN KEY (product_id)
            REFERENCES product_product(id) ON DELETE CASCADE,
        CONSTRAINT fk_psc_category FOREIGN KEY (category_id)
            REFERENCES product_category(id) ON DELETE CASCADE
    );
    CREATE INDEX idx_psc_category_id
        ON product_secondary_categories (category_id);
    CREATE INDEX idx_psc_product_id
        ON product_secondary_categories (product_id);
"""


@pytest.fixture
def secondary_categories_table(db):
    """Create the table PartsLogic owns and Saleor does not manage.

    Postgres DDL is transactional, so the table disappears again with the test
    transaction and every test that does not request this fixture sees the
    table as absent.
    """
    with connection.cursor() as cursor:
        cursor.execute(SECONDARY_CATEGORIES_DDL)


def add_secondary_category(product, category):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO product_secondary_categories (product_id, category_id) "
            "VALUES (%s, %s)",
            [product.pk, category.pk],
        )


def query_category_product_ids(api_client, category, channel):
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel.slug,
    }
    response = api_client.post_graphql(QUERY_CATEGORY_PRODUCTS, variables=variables)
    data = get_graphql_content(response)["data"]["category"]["products"]
    return data["totalCount"], [edge["node"]["id"] for edge in data["edges"]]


# The tail Django compiles for `exclude(category__in=<tree>)` on a nullable FK:
# `NOT (... IN (...) AND "category_id" IS NOT NULL)`. Nothing else in this
# statement produces it, so it is how the SQL guard below tells the disjointness
# anti-join apart from the plain membership predicates.
DISJOINTNESS_ANTI_JOIN_SQL = '"category_id" IS NOT NULL'
TREE_MEMBERSHIP_SQL = '"category_id" IN (SELECT'

QUERY_CATEGORY_PRODUCTS_PAGE = """
    query (
        $id: ID!
        $channel: String
        $first: Int
        $after: String
        $last: Int
        $before: String
        $sortBy: ProductOrder
        $filter: ProductFilterInput
        $where: ProductWhereInput
        $search: String
    ) {
        category(id: $id) {
            products(
                first: $first
                after: $after
                last: $last
                before: $before
                channel: $channel
                sortBy: $sortBy
                filter: $filter
                where: $where
                search: $search
            ) {
                totalCount
                pageInfo {
                    hasNextPage
                    hasPreviousPage
                    startCursor
                    endCursor
                }
                edges {
                    node {
                        slug
                    }
                }
            }
        }
    }
"""


def query_category_products(api_client, category, channel, **args):
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel.slug,
        **args,
    }
    response = api_client.post_graphql(
        QUERY_CATEGORY_PRODUCTS_PAGE, variables=variables
    )
    return get_graphql_content(response)["data"]["category"]["products"]


def query_category_product_slugs(api_client, category, channel, **args):
    data = query_category_products(api_client, category, channel, **args)
    return [edge["node"]["slug"] for edge in data["edges"]]


def raw_category_products(api_client, category, **args):
    """Return the unvalidated body, so a test can assert on `errors` itself."""
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        **args,
    }
    return api_client.post_graphql(
        QUERY_CATEGORY_PRODUCTS_PAGE, variables=variables
    ).json()


def walk_every_page_backward(api_client, category, channel, page_size, **args):
    """Page backwards through the whole connection, returning slugs in page order."""
    slugs: list[str] = []
    before = None
    for _ in range(50):
        data = query_category_products(
            api_client, category, channel, last=page_size, before=before, **args
        )
        slugs = [edge["node"]["slug"] for edge in data["edges"]] + slugs
        if not data["pageInfo"]["hasPreviousPage"]:
            return slugs
        before = data["pageInfo"]["startCursor"]
    raise AssertionError("backward pagination did not terminate")


def walk_every_page(api_client, category, channel, page_size, **args):
    """Page through the whole connection, returning the slugs in page order."""
    slugs: list[str] = []
    after = None
    for _ in range(50):  # loop guard, the fixtures are far smaller than this
        data = query_category_products(
            api_client, category, channel, first=page_size, after=after, **args
        )
        slugs.extend(edge["node"]["slug"] for edge in data["edges"])
        if not data["pageInfo"]["hasNextPage"]:
            return slugs
        after = data["pageInfo"]["endCursor"]
    raise AssertionError("pagination did not terminate")


def probe_statements_in(captured):
    """Return the availability probe's statements, in the order they ran."""
    return [
        query["sql"]
        for query in captured.captured_queries
        if "to_regclass" in query["sql"]
        or "has_table_privilege" in query["sql"]
        or "EXISTS (SELECT 1 FROM product_secondary_categories)" in query["sql"]
    ]


CATEGORY_UPDATE_WITH_PRODUCTS = """
    mutation ($id: ID!, $name: String!, $channel: String) {
        categoryUpdate(id: $id, input: {name: $name}) {
            errors { field message }
            category {
                products(first: 20, channel: $channel) {
                    edges { node { slug } }
                }
            }
        }
    }
"""

CATEGORY_UPDATE_TOTAL_COUNT_ONLY = """
    mutation ($id: ID!, $name: String!, $channel: String) {
        categoryUpdate(id: $id, input: {name: $name}) {
            errors { field message }
            category { products(channel: $channel) { totalCount } }
        }
    }
"""


def move_to_own_category(product, slug):
    """Give `product` a primary category outside any tree under test."""
    category = Category.objects.create(name=slug, slug=slug)
    product.category = category
    product.save(update_fields=["category"])
    return category


def test_secondary_category_product_is_listed(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a product whose primary category is somewhere else entirely
    category = Category.objects.first()
    elsewhere = Category.objects.create(name="Elsewhere", slug="elsewhere")
    guest = product_list[0]
    guest.category = elsewhere
    guest.save(update_fields=["category"])

    # when it is listed as a secondary member of the queried category
    add_secondary_category(guest, category)
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then it shows up in the listing and in the count
    assert total_count == 3
    assert graphene.Node.to_global_id("Product", guest.pk) in product_ids
    assert len(product_ids) == 3


def test_secondary_category_product_on_descendant_is_listed(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a product outside the tree, secondary-assigned to a CHILD category
    category = Category.objects.first()
    child = Category.objects.create(name="Child", slug="child", parent=category)
    elsewhere = Category.objects.create(name="Elsewhere", slug="elsewhere")
    guest = product_list[0]
    guest.category = elsewhere
    guest.save(update_fields=["category"])
    add_secondary_category(guest, child)

    # when the parent category is queried
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then the descendant's secondary member is included
    assert total_count == 3
    assert graphene.Node.to_global_id("Product", guest.pk) in product_ids


def test_product_in_both_primary_and_secondary_is_returned_once(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a product that is both a primary and a secondary member
    category = Category.objects.first()
    add_secondary_category(product_list[0], category)

    # when the category is queried
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then it appears exactly once
    assert total_count == 3
    assert len(product_ids) == 3
    assert len(set(product_ids)) == 3


def test_unpublished_secondary_category_product_is_hidden_from_customer(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given an unpublished product secondary-assigned to the queried category
    category = Category.objects.first()
    elsewhere = Category.objects.create(name="Elsewhere", slug="elsewhere")
    guest = product_list[0]
    guest.category = elsewhere
    guest.save(update_fields=["category"])
    guest.channel_listings.all().update(is_published=False)
    add_secondary_category(guest, category)

    # when a customer queries the category
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then publication filtering still applies to the secondary leg
    assert total_count == 2
    assert graphene.Node.to_global_id("Product", guest.pk) not in product_ids


def test_empty_table_degrades_to_primary_categories_only(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the table exists but holds no rows, as on a tenant that ran the
    # PartsLogic migrations and never loaded a secondary assignment
    category = Category.objects.first()
    elsewhere = Category.objects.create(name="Elsewhere", slug="elsewhere")
    guest = product_list[0]
    guest.category = elsewhere
    guest.save(update_fields=["category"])

    # when the category is queried
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then the probe is false and the resolver answers as it did before the patch
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is False
    )
    assert total_count == 2
    assert graphene.Node.to_global_id("Product", guest.pk) not in product_ids


def test_missing_table_degrades_to_primary_categories_only(
    user_api_client, product_list, channel_USD
):
    # given no `product_secondary_categories` table in this database
    category = Category.objects.first()
    elsewhere = Category.objects.create(name="Elsewhere", slug="elsewhere")
    guest = product_list[0]
    guest.category = elsewhere
    guest.save(update_fields=["category"])

    # when the category is queried
    total_count, product_ids = query_category_product_ids(
        user_api_client, category, channel_USD
    )

    # then the resolver answers exactly as it did before the patch
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is False
    )
    assert total_count == 2
    assert graphene.Node.to_global_id("Product", guest.pk) not in product_ids


@pytest.mark.parametrize("page_size", [1, 2, 3])
def test_cursor_walk_across_legs_has_no_duplicates_and_no_gaps(
    page_size, user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given one product in the secondary leg only, one in both legs, one primary
    category = Category.objects.first()
    secondary_only, both, primary_only = product_list
    move_to_own_category(secondary_only, "somewhere-else")
    add_secondary_category(secondary_only, category)
    add_secondary_category(both, category)

    # when the whole connection is paged through, page boundary by page boundary
    slugs = walk_every_page(user_api_client, category, channel_USD, page_size)

    # then every product appears exactly once, in slug order
    assert slugs == [
        secondary_only.slug,
        both.slug,
        primary_only.slug,
    ]


def test_total_count_matches_the_union(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a union of one secondary-only product and two primary ones, where
    # one of the primary ones is also a secondary member
    category = Category.objects.first()
    secondary_only, both, _primary_only = product_list
    move_to_own_category(secondary_only, "somewhere-else")
    add_secondary_category(secondary_only, category)
    add_secondary_category(both, category)

    # when the count and the edges are read
    data = query_category_products(user_api_client, category, channel_USD, first=20)

    # then the count is the distinct union, not the sum of the legs' memberships
    assert data["totalCount"] == 3
    assert len(data["edges"]) == 3


def test_sorting_by_price_interleaves_the_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the secondary-leg product priced between the two primary ones
    # (10, 20, 30 in fixture order, so the middle one goes to the other leg)
    category = Category.objects.first()
    cheapest, middle, dearest = product_list
    move_to_own_category(middle, "somewhere-else")
    add_secondary_category(middle, category)

    # when sorted by price ascending, one page at a time
    slugs = walk_every_page(
        user_api_client,
        category,
        channel_USD,
        1,
        sortBy={"field": "PRICE", "direction": "ASC"},
    )

    # then the secondary-leg product lands in the middle, not at either end
    assert slugs == [cheapest.slug, middle.slug, dearest.slug]


def test_sorting_by_product_type_orders_across_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table, product_type
):
    # given the secondary-leg product on a product type that sorts first
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    first_by_type = product_type.__class__.objects.create(
        name="AAA type", slug="aaa-type", kind=product_type.kind
    )
    guest.product_type = first_by_type
    guest.save(update_fields=["product_type"])

    # when sorted by a field that lives on a joined table
    slugs = query_category_product_slugs(
        user_api_client,
        category,
        channel_USD,
        first=20,
        sortBy={"field": "TYPE", "direction": "ASC"},
    )

    # then the join is resolved for the merged result, not only per leg
    assert slugs[0] == guest.slug
    assert len(slugs) == 3


def test_sorting_by_attribute_returns_the_whole_union(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    product_type,
):
    # given a secondary-leg product and an attribute to sort by
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    attribute = product_type.product_attributes.first()

    # when sorted by attribute values, which groups and aggregates per product
    slugs = query_category_product_slugs(
        user_api_client,
        category,
        channel_USD,
        first=20,
        sortBy={
            "attributeId": graphene.Node.to_global_id("Attribute", attribute.pk),
            "direction": "ASC",
        },
    )

    # then the aggregation does not drop or duplicate either leg
    assert sorted(slugs) == sorted(product.slug for product in product_list)


def test_backwards_pagination_across_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a union whose last product by slug sits in the secondary leg
    category = Category.objects.first()
    last_by_slug = product_list[2]
    move_to_own_category(last_by_slug, "somewhere-else")
    add_secondary_category(last_by_slug, category)

    # when the last page is requested
    data = query_category_products(user_api_client, category, channel_USD, last=1)

    # then the reversed per-leg ordering still agrees with the merged ordering
    assert [edge["node"]["slug"] for edge in data["edges"]] == [last_by_slug.slug]
    assert data["pageInfo"]["hasPreviousPage"] is True


def test_filter_applies_to_the_secondary_leg(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the cheapest product reaching the category only as a secondary member
    category = Category.objects.first()
    cheapest = product_list[0]
    move_to_own_category(cheapest, "somewhere-else")
    add_secondary_category(cheapest, category)

    # ... and reaching it: without a filter the union is all three
    assert query_category_product_slugs(
        user_api_client, category, channel_USD, first=20
    ) == [product.slug for product in product_list]

    # when a price filter that excludes it is applied
    data = query_category_products(
        user_api_client,
        category,
        channel_USD,
        first=20,
        filter={"price": {"gte": 15}},
    )

    # then the secondary leg is filtered too, in the edges and in the count
    assert [edge["node"]["slug"] for edge in data["edges"]] == [
        product_list[1].slug,
        product_list[2].slug,
    ]
    assert data["totalCount"] == 2


def test_where_filter_applies_to_the_secondary_leg(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-only product excluded by a `where` slug filter
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # ... and reaching it: without a filter the union is all three
    assert query_category_product_slugs(
        user_api_client, category, channel_USD, first=20
    ) == [product.slug for product in product_list]

    # when the `where` argument names the other two products
    data = query_category_products(
        user_api_client,
        category,
        channel_USD,
        first=20,
        where={"slug": {"oneOf": [product_list[1].slug, product_list[2].slug]}},
    )

    # then the secondary leg honours it
    assert [edge["node"]["slug"] for edge in data["edges"]] == [
        product_list[1].slug,
        product_list[2].slug,
    ]
    assert data["totalCount"] == 2


def test_search_applies_to_the_secondary_leg(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-only product that does not match the search term, and a
    # primary product that does ("big orange product" is the second fixture)
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # ... and reaching it: without a search term the union is all three
    assert query_category_product_slugs(
        user_api_client, category, channel_USD, first=20
    ) == [product.slug for product in product_list]

    # when the category is searched
    data = query_category_products(
        user_api_client, category, channel_USD, first=20, search="orange"
    )

    # then the secondary leg is searched, not merely appended
    assert [edge["node"]["slug"] for edge in data["edges"]] == [product_list[1].slug]
    assert data["totalCount"] == 1


def test_search_finds_a_secondary_only_product(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the only product matching the search term reachable via the
    # secondary table alone
    category = Category.objects.first()
    guest = product_list[1]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when the category is searched for a term only that product matches
    data = query_category_products(
        user_api_client, category, channel_USD, first=20, search="orange"
    )

    # then it is found through the secondary leg
    assert [edge["node"]["slug"] for edge in data["edges"]] == [guest.slug]
    assert data["totalCount"] == 1


def test_secondary_only_category_lists_every_secondary_product(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a category with no primary products at all, the dmk vehicle-tree
    # shape: every product reaches it only through the secondary table
    vehicle_tree = Category.objects.create(name="Jeep", slug="jeep")
    child = Category.objects.create(name="Axles", slug="axles", parent=vehicle_tree)
    for product in product_list:
        add_secondary_category(product, child)

    # when the ancestor is queried
    data = query_category_products(user_api_client, vehicle_tree, channel_USD, first=20)

    # then the whole set surfaces, where today it is an empty page
    assert data["totalCount"] == 3
    assert [edge["node"]["slug"] for edge in data["edges"]] == [
        product.slug for product in product_list
    ]


def test_primary_only_category_is_unchanged_while_the_table_holds_rows(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a populated table whose rows all point at a different category
    category = Category.objects.first()
    other = Category.objects.create(name="Other", slug="other")
    for product in product_list:
        add_secondary_category(product, other)

    # when a category with only primary members is queried
    data = query_category_products(user_api_client, category, channel_USD, first=20)

    # then it answers exactly as it did before the patch, with the feature ON
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )
    assert data["totalCount"] == 3
    assert [edge["node"]["slug"] for edge in data["edges"]] == [
        product.slug for product in product_list
    ]


def test_probe_is_false_without_the_select_privilege(secondary_categories_table):
    # given a role that can see the table in the catalog but cannot read it
    with connection.cursor() as cursor:
        cursor.execute("CREATE ROLE probe_without_select")
        cursor.execute("GRANT probe_without_select TO CURRENT_USER")
        cursor.execute("SET ROLE probe_without_select")
        try:
            # when the probe runs, it neither raises nor claims the feature
            assert (
                secondary_categories_available(
                    settings.DATABASE_CONNECTION_DEFAULT_NAME
                )
                is False
            )
        finally:
            cursor.execute("RESET ROLE")


def test_emitted_sql_keeps_the_per_leg_limit_and_the_plan_fence(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the two-leg shape active on a category with both kinds of member
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when a page and its count are resolved
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        query_category_products(user_api_client, category, channel_USD, first=20)
    statements = [query["sql"] for query in ctx.captured_queries]

    # then the page is ONE statement carrying a LIMIT per leg plus the outer one,
    # with no DISTINCT (the legs are disjoint) and no N+1 per product
    page_statements = [sql for sql in statements if "UNION ALL" in sql]
    assert len(page_statements) == 1
    page_sql = page_statements[0]
    assert page_sql.count("LIMIT 21") == 3
    assert "DISTINCT" not in page_sql

    # ... and the secondary leg keeps the ARRAY() optimization fence, without
    # which Postgres inverts the semi-join and walks the whole slug index
    assert "= ANY (ARRAY(" in page_sql

    # ... and both membership predicates are present, including the anti-join
    # that makes the legs disjoint. `count()` requires it; the page does not, but
    # removing it measures SLOWER at scale (it is what makes the secondary leg
    # selective), so it stays on both paths
    assert TREE_MEMBERSHIP_SQL in page_sql
    assert DISJOINTNESS_ANTI_JOIN_SQL in page_sql

    # ... and totalCount is two indexed counts, never one scan of the union
    count_statements = [sql for sql in statements if "COUNT(*)" in sql]
    assert len(count_statements) == 2
    assert all("UNION ALL" not in sql for sql in count_statements)

    # ... and exactly one of them carries the anti-join, which is what makes the
    # legs disjoint and therefore makes their sum the union count
    assert [DISJOINTNESS_ANTI_JOIN_SQL in sql for sql in count_statements] == [
        False,
        True,
    ]


# ---------------------------------------------------------------------------
# The tests below are folded in from a cold adversarial review of this branch
# (2026-08-17, reviewer's probes A through M, preserved verbatim in intent). Two
# of them found real defects: the `COLLECTION` sort 500 and the one-way probe
# cache. They are kept because they are the checks that caught those, not
# because they pass.
# ---------------------------------------------------------------------------

# Every value of `ProductOrderField`. Hard-coded rather than derived so that an
# upstream pull adding a value fails the exhaustiveness assertion below and gets
# swept deliberately, instead of being skipped silently.
ALL_PRODUCT_SORT_FIELDS = [
    "NAME",
    "PRICE",
    "MINIMAL_PRICE",
    "LAST_MODIFIED",
    "DATE",
    "TYPE",
    "PUBLISHED",
    "PUBLICATION_DATE",
    "PUBLISHED_AT",
    "LAST_MODIFIED_AT",
    "COLLECTION",
    "RATING",
    "CREATED_AT",
    # RANK is only legal alongside `search`, enforced by
    # validate_and_apply_search_rank_sorting, so the sweeps below supply one.
    "RANK",
]


def sort_field_extra_args(field):
    return {"search": "product"} if field == "RANK" else {}


def test_the_swept_sort_fields_are_every_sort_field():
    # given the enum upstream defines
    from ...graphql.product.sorters import ProductOrderField

    # then the sweep below covers all of it, or this fails and says so
    assert sorted(ALL_PRODUCT_SORT_FIELDS) == sorted(
        ProductOrderField._meta.enum.__members__
    )
    # ... and every sort still terminates in a unique tiebreak (slug/pk/id), which
    # is what makes each ordering TOTAL. The per-leg top-k proof rests on totality,
    # not on the set of names, so this asserts the enum VALUES: an upstream pull
    # that drops a sort's `slug`/`pk`/`id` tail (e.g. TYPE losing its final "slug")
    # keeps every name and still fails here, instead of silently returning a wrong
    # row at a page boundary. See FORK-NOTES rebase hazard 6.
    assert all(
        member.value[-1] in ("slug", "pk", "id")
        for member in ProductOrderField._meta.enum.__members__.values()
    ), {
        name: member.value
        for name, member in ProductOrderField._meta.enum.__members__.items()
    }


def test_the_category_shaped_filter_fields_are_the_ones_we_widen():
    # given the product filter and where surfaces
    from ...graphql.product.filters.product import ProductFilter, ProductWhere

    # then the category-shaped fields are exactly the ones FORK-NOTES documents as
    # widening with the feature on. This is the mirror of the sort tripwire above:
    # an upstream pull that adds a new category-shaped filter widens it untested and
    # fails here, instead of the widening being discovered on a live tenant.
    assert {name for name in ProductFilter.base_filters if "categor" in name} == {
        "categories",
        "has_category",
    }
    assert {name for name in ProductWhere.base_filters if "categor" in name} == {
        "category",
        "has_category",
    }


@pytest.mark.parametrize("field", ALL_PRODUCT_SORT_FIELDS)
def test_every_sort_field_works_with_the_feature_on(
    field, user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a union of one secondary-only product and two primary ones
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when the category is sorted by every field the schema accepts
    body = raw_category_products(
        user_api_client,
        category,
        channel=channel_USD.slug,
        first=20,
        sortBy={"field": field, "direction": "ASC"},
        **sort_field_extra_args(field),
    )

    # then none of them errors, and each returns the whole union
    assert "errors" not in body, f"{field}: {body['errors'][0]['message']}"
    data = body["data"]["category"]["products"]
    assert data["totalCount"] == 3, (field, data)
    assert len(data["edges"]) == 3, (field, data)


@pytest.mark.parametrize("field", ALL_PRODUCT_SORT_FIELDS)
def test_every_sort_field_works_with_the_feature_off(
    field, user_api_client, product_list, channel_USD
):
    # given no table, so the resolver is on the pre-patch path
    category = Category.objects.first()

    # when the same sweep runs
    body = raw_category_products(
        user_api_client,
        category,
        channel=channel_USD.slug,
        first=20,
        sortBy={"field": field, "direction": "ASC"},
        **sort_field_extra_args(field),
    )

    # then it behaves the same, which is what makes the sweep above a regression
    # test rather than a statement about upstream
    assert "errors" not in body, f"{field}: {body['errors'][0]['message']}"
    assert body["data"]["category"]["products"]["totalCount"] == 3


def test_multi_leg_queryset_interface_is_closed():
    # given the stand-in the connection machinery is handed
    from ..secondary_categories import MultiLegQuerySet

    legs = MultiLegQuerySet(Category.objects.all(), [Category.objects.all()])

    # then a method it does not explicitly implement is a plain AttributeError,
    # never a silently wrapped return value: that wrapping is what turned
    # `qs_with_collection`'s `aggregate()` into a request-time TypeError
    assert not hasattr(MultiLegQuerySet, "__getattr__")
    with pytest.raises(AttributeError):
        legs.exists()
    with pytest.raises(AttributeError):
        legs.first()

    # ... while `aggregate` returns a real dict rather than another wrapper
    assert legs.aggregate(low=Min("pk")) == {
        "low": Category.objects.aggregate(low=Min("pk"))["low"]
    }


def test_secondary_product_with_null_primary_category_is_listed(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-assigned product whose primary category is NULL
    # (`Product.category` is nullable with on_delete=SET_NULL)
    category = Category.objects.first()
    guest = product_list[0]
    guest.category = None
    guest.save(update_fields=["category"])
    add_secondary_category(guest, category)

    # when the category is queried
    data = query_category_products(user_api_client, category, channel_USD, first=20)

    # then the NULL primary category does not drop it from either leg
    assert guest.slug in [edge["node"]["slug"] for edge in data["edges"]]
    assert data["totalCount"] == 3


def test_paged_search_walk_across_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-only product and a search term all three match
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when the whole connection is walked one product at a time under search,
    # which puts the connection on the search_rank epsilon cursor path
    slugs = walk_every_page(user_api_client, category, channel_USD, 1, search="product")

    # then every product appears exactly once
    assert sorted(slugs) == sorted(product.slug for product in product_list)
    assert len(slugs) == len(set(slugs))


def test_paged_walk_with_a_null_sort_key_across_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the secondary-leg product's sort key NULL and the primary ones set
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    product_list[1].rating = 5
    product_list[1].save(update_fields=["rating"])
    product_list[2].rating = 1
    product_list[2].save(update_fields=["rating"])

    # when the connection is walked one at a time sorted by that key
    slugs = walk_every_page(
        user_api_client,
        category,
        channel_USD,
        1,
        sortBy={"field": "RATING", "direction": "ASC"},
    )

    # then NULL placement is identical across the legs and the outer query, so
    # nothing is skipped or repeated at a page boundary
    assert sorted(slugs) == sorted(product.slug for product in product_list)
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize("page_size", [1, 2])
def test_full_backwards_walk_across_legs(
    page_size, user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given one product reachable only through the secondary table and one in both
    category = Category.objects.first()
    secondary_only, both, _primary_only = product_list
    move_to_own_category(secondary_only, "somewhere-else")
    add_secondary_category(secondary_only, category)
    add_secondary_category(both, category)

    # when the connection is walked backwards with `last` and `before`
    slugs = walk_every_page_backward(user_api_client, category, channel_USD, page_size)

    # then the reversed per-leg ordering agrees with the merged ordering
    assert slugs == [product.slug for product in product_list]


def test_staff_without_a_channel_argument_sees_the_secondary_leg(
    staff_api_client,
    permission_manage_products,
    product_list,
    secondary_categories_table,
):
    # given a staff requestor with no channel, so the resolver takes the branch
    # that applies neither the visibility filter nor the annotations
    staff_api_client.user.user_permissions.add(permission_manage_products)
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when the category is queried
    body = raw_category_products(staff_api_client, category, first=20)

    # then removing the category filter from that branch is correctly compensated
    assert "errors" not in body, body["errors"][0]["message"]
    data = body["data"]["category"]["products"]
    assert guest.slug in [edge["node"]["slug"] for edge in data["edges"]]
    assert data["totalCount"] == 3


def test_secondary_rows_on_both_parent_and_child_yield_one_product(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the same product secondary-assigned twice inside one tree
    category = Category.objects.first()
    child = Category.objects.create(name="Child", slug="child", parent=category)
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    add_secondary_category(guest, child)

    # when the ancestor is queried
    data = query_category_products(user_api_client, category, channel_USD, first=20)

    # then the membership test is set-like, in the page and in the count
    slugs = [edge["node"]["slug"] for edge in data["edges"]]
    assert slugs.count(guest.slug) == 1
    assert data["totalCount"] == 3


def test_where_with_the_and_operator_reaches_both_legs(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-only product and a nested `where` input
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when the operator form of `where` is used
    body = raw_category_products(
        user_api_client,
        category,
        channel=channel_USD.slug,
        first=20,
        where={
            "AND": [
                {"slug": {"oneOf": [product.slug for product in product_list]}},
                {"isPublished": True},
            ]
        },
    )

    # then it applies to both legs, once
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 3


QUERY_CATEGORY_TOTAL_COUNT_ONLY = """
    query ($id: ID!, $channel: String) {
        category(id: $id) { products(channel: $channel) { totalCount } }
    }
"""


def test_total_count_without_an_edges_selection(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a query asking only for the count, so `first`/`last` are absent and
    # the connection never materializes a page
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # when it is resolved
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        body = user_api_client.post_graphql(
            QUERY_CATEGORY_TOTAL_COUNT_ONLY,
            variables={
                "id": graphene.Node.to_global_id("Category", category.pk),
                "channel": channel_USD.slug,
            },
        ).json()

    # then the un-limited slice the merge would build is never executed
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 3
    assert not [
        query["sql"] for query in ctx.captured_queries if "UNION ALL" in query["sql"]
    ]


def test_table_dropped_after_the_probe_cached_true_degrades(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a warm probe result while the table is present
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )

    # when the table goes away underneath a running process, which PartsLogic's
    # migrator, a REVOKE, a schema swap or a failover can all do
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # then the next request re-probes and degrades to pre-patch behavior instead
    # of raising ProgrammingError on every category page until a restart
    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_table_populated_after_the_probe_cached_false_starts_working(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a warm probe result taken while the table was empty
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 2
    )

    # when the ETL loads its first row, without restarting Saleor
    add_secondary_category(guest, category)

    # then the feature turns itself on
    data = query_category_products(user_api_client, category, channel_USD, first=20)
    assert data["totalCount"] == 3
    assert guest.slug in [edge["node"]["slug"] for edge in data["edges"]]


def test_probe_result_is_cached_for_the_ttl(monkeypatch, secondary_categories_table):
    # given the real TTL rather than the per-test zero
    monkeypatch.setattr(wsm_models, "PROBE_TTL_SECONDS", 60)
    clear_secondary_categories_cache()
    alias = settings.DATABASE_CONNECTION_DEFAULT_NAME
    probes = []
    real_probe = wsm_models._probe_secondary_categories

    def counting_probe(name):
        probes.append(name)
        return real_probe(name)

    monkeypatch.setattr(wsm_models, "_probe_secondary_categories", counting_probe)

    # when it is asked twice in the same interval
    first = secondary_categories_available(alias)
    second = secondary_categories_available(alias)

    # then the database is touched once, which is the point of the cache
    assert (first, second) == (False, False)
    assert probes == [alias]


def test_deleting_a_product_cascades_its_secondary_rows_away(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given a secondary-only product in the listing
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )

    # when the product is deleted, which the model deliberately does not
    # participate in (plain BigIntegerFields, no reverse accessor)
    guest.delete()

    # then the database's own ON DELETE CASCADE removed the row, so the listing
    # neither counts nor references a product that no longer exists
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM product_secondary_categories")
        assert cursor.fetchone()[0] == 0
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 2
    )


# ---------------------------------------------------------------------------
# A second cold adversarial review (round 2, at 6f9be35) found that the fixture
# above pinned the probe TTL to zero module-wide, so the shipped cache path was
# the one thing never exercised: with a cached True and the table then dropped,
# every category page returned Internal Server Error for the rest of the
# interval. The tests below run at the real TTL and pin the degradation
# behaviour, plus the collection-sentinel ordering bug the same review found.
# ---------------------------------------------------------------------------


def test_real_ttl_degrades_when_the_table_vanishes(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a probe result cached at the TTL production runs
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )

    # when PartsLogic's migrator drops the table underneath the running process,
    # so the cached True is now wrong and no re-probe is due for a full interval
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # then the request is answered from the primary category alone rather than
    # raising, which is exactly what upstream would have returned
    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )
    assert "errors" not in body, body["errors"][0]["message"]
    data = body["data"]["category"]["products"]
    assert data["totalCount"] == 2
    assert guest.slug not in [edge["node"]["slug"] for edge in data["edges"]]

    # ... and the failure, being more authoritative than any probe, has already
    # turned the feature off, so the next request emits upstream's SQL
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is False
    )
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        query_category_products(user_api_client, category, channel_USD, first=20)
    statements = [query["sql"] for query in ctx.captured_queries]
    assert not [sql for sql in statements if "secondary_categories" in sql]
    assert not [sql for sql in statements if "UNION ALL" in sql]


def test_real_ttl_degrades_a_total_count_only_query_when_the_table_vanishes(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given the same stale-True cache, but a query that asks only for the count.
    # `totalCount` resolves from a callable after the resolver has returned, so
    # the page statement never runs and this is the only place its failure can
    # be caught.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel_USD.slug,
    }
    assert (
        get_graphql_content(
            user_api_client.post_graphql(
                QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
            )
        )["data"]["category"]["products"]["totalCount"]
        == 3
    )

    # when the table goes away
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # then the count degrades to the primary leg instead of raising
    body = user_api_client.post_graphql(
        QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
    ).json()
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_real_ttl_degrades_when_the_table_is_renamed(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a probe cached True and PartsLogic then renaming the table out from
    # under it, which its migrator does on a rebuild. Same `ProgrammingError`
    # class as a revoked SELECT grant, which the probe-level test covers.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE product_secondary_categories "
            "RENAME TO product_secondary_categories_old"
        )

    # when the next request runs against the stale cached True
    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )

    # then it degrades rather than raising
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_degradation_leaves_the_connection_usable(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a request that had to degrade mid-flight, inside a transaction block
    # (which is where a failed statement poisons everything after it)
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    query_category_products(user_api_client, category, channel_USD, first=20)
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")
    raw_category_products(user_api_client, category, channel=channel_USD.slug, first=20)

    # then ordinary ORM work still functions, i.e. the savepoint really rolled the
    # aborted statement back rather than leaving the block poisoned
    assert Category.objects.count() >= 1
    assert Category.objects.create(name="After", slug="after").pk


def test_collection_sort_sentinel_covers_the_whole_union(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    published_collection,
):
    # given a secondary-leg product ordered BELOW every primary-leg product in
    # the collection, so a sentinel computed from the primary leg alone would
    # land in the middle of the result instead of underneath it
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    published_collection.products.add(guest, product_list[1])
    collection_products = published_collection.collectionproduct.all()
    for collection_product in collection_products:
        collection_product.sort_order = (
            1 if collection_product.product_id == guest.pk else 9
        )
        collection_product.save(update_fields=["sort_order"])

    # when the category is sorted by collection order
    slugs = query_category_product_slugs(
        user_api_client,
        category,
        channel_USD,
        first=20,
        sortBy={"field": "COLLECTION", "direction": "ASC"},
    )

    # then the product with no collection ordering at all sorts below neither
    # more nor less than it would upstream: ahead of both ordered products,
    # because the sentinel is Min(sort_order) - 1 over everything reachable
    assert slugs[0] == product_list[2].slug, slugs
    assert slugs[1:] == [guest.slug, product_list[1].slug], slugs


# ---------------------------------------------------------------------------
# A third cold adversarial review (round 3, at 7cdb9f4) attacked the error
# handling the round-2 fixes added, and found that `except ProgrammingError` was
# scoped to nothing: an unrelated failure anywhere inside the union path was
# answered as a silently truncated page at HTTP 200 and turned the feature off
# process-wide, with the real error never reaching a log. Its probes are folded
# in below, several of them INVERTED (they asserted the defect; they now assert
# the fix), together with its 40 paginated walks.
# ---------------------------------------------------------------------------


@contextmanager
def fail_statement(alias, needle, error):
    """Raise `error` from the first statement whose SQL contains `needle`."""
    state = {"fired": False}

    def wrapper(execute, sql, params, many, context):
        if not state["fired"] and needle in sql:
            state["fired"] = True
            raise error
        return execute(sql, params, many, context)

    with connections[alias].execute_wrapper(wrapper):
        yield state


def driver_error(error, sqlstate):
    """Attach a driver-style SQLSTATE, the way Django chains the real thing."""

    class Driver(Exception):
        pass

    cause = Driver(sqlstate)
    cause.sqlstate = sqlstate
    error.__cause__ = cause
    return error


@contextmanager
def fail_statement_where(alias, matches, error):
    """Raise `error` from the first statement `matches` accepts."""
    state = {"fired": False}

    def wrapper(execute, sql, params, many, context):
        if not state["fired"] and matches(sql):
            state["fired"] = True
            raise error
        return execute(sql, params, many, context)

    with connections[alias].execute_wrapper(wrapper):
        yield state


TWO_CATEGORIES = """
    query ($a: ID!, $b: ID!, $channel: String) {
        first: category(id: $a) {
            products(first: 20, channel: $channel) {
                totalCount
                edges { node { slug } }
            }
        }
        second: category(id: $b) {
            products(first: 20, channel: $channel) {
                totalCount
                edges { node { slug } }
            }
        }
    }
"""


@contextmanager
def drop_table_before(alias, needle):
    """Drop the table immediately before the statement matching `needle` runs.

    Reproduces the real race: PartsLogic's migrator dropping the table between
    two of the probe's three statements.
    """
    state = {"fired": False}

    def wrapper(execute, sql, params, many, context):
        if not state["fired"] and needle in sql:
            state["fired"] = True
            with allow_writer(), connections[alias].cursor() as cursor:
                cursor.execute("DROP TABLE product_secondary_categories")
        return execute(sql, params, many, context)

    with connections[alias].execute_wrapper(wrapper):
        yield state


def test_a_failure_shared_with_the_fallback_surfaces_and_leaves_the_probe_alone(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given the deployment state this fork explicitly warns about: new code
    # against a database that has not run its migrations, so a column the query
    # names does not exist. The SQLSTATE (42703) is one this code treats as
    # "the secondary table was rebuilt", so only the fallback can tell them apart.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE product_product RENAME COLUMN description_plaintext TO gone"
        )

    # when a request runs
    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )

    # then the fallback fails the same way, so the real error surfaces instead of
    # being reported as a secondary-table problem, and the feature is left on
    assert "errors" in body, f"a shared failure was swallowed: {body['data']}"
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )


def test_an_operational_error_from_the_secondary_leg_still_surfaces(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a deadlock (OperationalError) on a statement that DOES name the table
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    # `= ANY (ARRAY(` appears only in the page and count statements, never in the
    # probe, so this targets the leg rather than the availability check
    with fail_statement(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        "= ANY (ARRAY(",
        OperationalError("deadlock detected"),
    ) as state:
        body = raw_category_products(
            user_api_client, category, channel=channel_USD.slug, first=20
        )

    # then "nothing broader is caught" holds: a transient failure is not an
    # availability verdict and must not be silently degraded
    assert state["fired"], "the secondary leg never ran"
    assert "errors" in body, "a deadlock was silently degraded"


def test_a_programming_error_without_a_sqlstate_from_the_leg_still_surfaces(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a ProgrammingError raised BY the leg statement, so structural
    # narrowing cannot help, but carrying no SQLSTATE. Degrading needs the
    # driver's own verdict that the table is the problem; without it there is
    # nothing to distinguish this from any other broken query.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    with fail_statement(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        "= ANY (ARRAY(",
        ProgrammingError("something else entirely"),
    ) as state:
        body = raw_category_products(
            user_api_client, category, channel=channel_USD.slug, first=20
        )

    # then it surfaces, and the feature is left on
    assert state["fired"], "the secondary leg never ran"
    assert "errors" in body, f"an unexplained error was swallowed: {body['data']}"
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )


def test_a_transient_failure_during_the_probe_degrades_rather_than_erroring(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a deadlock on the probe's own population check. This is a statement
    # the fork ADDED, so it must never turn a transient blip into a 500 upstream
    # would not have had; the probe fails safe and the TTL heals it.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    with fail_statement(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        "EXISTS (SELECT 1 FROM product_secondary_categories)",
        OperationalError("deadlock detected"),
    ) as state:
        body = raw_category_products(
            user_api_client, category, channel=channel_USD.slug, first=20
        )

    # then the request is answered as upstream would have answered it, not raised
    assert state["fired"], "the population probe never ran"
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_degrades_when_the_table_is_rebuilt_with_a_different_shape(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a rebuild that renames the column the fence reads, which is
    # SQLSTATE 42703 rather than the 42P01 a drop produces
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE product_secondary_categories RENAME COLUMN product_id TO gone"
        )

    # when the next request runs against the stale cached True
    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )

    # then it degrades, because the fallback proves the rest of the query is fine
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_a_probe_failure_inside_a_transaction_block_still_degrades(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given the table disappearing between the probe's privilege check and its
    # population check, so the probe's own `except DatabaseError` fires
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    with drop_table_before(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        "EXISTS (SELECT 1 FROM product_secondary_categories)",
    ) as state:
        body = raw_category_products(
            user_api_client, category, channel=channel_USD.slug, first=20
        )

    # then the swallowed failure did not leave the transaction block aborted, so
    # upstream's primary-only query still runs and the field is answered
    assert state["fired"], "the population probe never ran"
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2


def test_degrades_inside_a_mutation_response_page_path(
    staff_api_client,
    permission_manage_products,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a `Category.products` edges selection inside a mutation payload,
    # which is the live case that resolves on the writer alias inside an open
    # transaction block, so the savepoint is load-bearing
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    staff_api_client.user.user_permissions.add(permission_manage_products)
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "name": "Renamed once",
        "channel": channel_USD.slug,
    }
    warm = get_graphql_content(
        staff_api_client.post_graphql(CATEGORY_UPDATE_WITH_PRODUCTS, variables)
    )["data"]["categoryUpdate"]["category"]["products"]
    assert len(warm["edges"]) == 3

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # when the mutation runs again against the stale cached True
    variables["name"] = "Renamed twice"
    body = staff_api_client.post_graphql(
        CATEGORY_UPDATE_WITH_PRODUCTS, variables
    ).json()

    # then the payload degrades and the mutation's own write survives the
    # savepoint rollback
    assert "errors" not in body, body["errors"][0]["message"]
    products = body["data"]["categoryUpdate"]["category"]["products"]
    assert len(products["edges"]) == 2
    assert guest.slug not in [edge["node"]["slug"] for edge in products["edges"]]
    category.refresh_from_db()
    assert category.name == "Renamed twice"


@pytest.mark.parametrize("table_present", [True, False])
def test_total_count_in_a_mutation_response_is_upstream_parity(
    staff_api_client,
    permission_manage_products,
    product_list,
    channel_USD,
    db,
    table_present,
    real_probe_ttl,
):
    """`totalCount` in a mutation payload fails identically with and without the fork.

    `create_connection_slice` returns it as a callable graphene resolves after
    the resolver has returned, so outside `allow_writer_in_context`. On the writer
    alias that trips `restrict_writer`, upstream included. Kept as the control
    that separates inherited behaviour from a fork regression, because the fork's
    own docs used to cite this case as live.
    """
    category = Category.objects.first()
    if table_present:
        with connection.cursor() as cursor:
            cursor.execute(SECONDARY_CATEGORIES_DDL)
        add_secondary_category(product_list[0], category)
    staff_api_client.user.user_permissions.add(permission_manage_products)
    body = staff_api_client.post_graphql(
        CATEGORY_UPDATE_TOTAL_COUNT_ONLY,
        {
            "id": graphene.Node.to_global_id("Category", category.pk),
            "name": "Count once",
            "channel": channel_USD.slug,
        },
    ).json()

    assert "errors" in body, "expected the inherited writer restriction to bite"
    assert (
        body["errors"][0]["extensions"]["exception"]["code"]
        == "UnsafeWriterAccessError"
    ), body["errors"]


def test_degrades_under_rank_sorting(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given RANK sorting, whose cursor is the special-cased two-element rank shape
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    query_category_products(
        user_api_client,
        category,
        channel_USD,
        first=20,
        search="product",
        sortBy={"field": "RANK", "direction": "DESC"},
    )
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # when the table has gone and the cached probe still says otherwise
    body = raw_category_products(
        user_api_client,
        category,
        channel=channel_USD.slug,
        first=20,
        search="product",
        sortBy={"field": "RANK", "direction": "DESC"},
    )

    # then the degradation survives the unusual cursor shape
    assert "errors" not in body, body["errors"][0]["message"]


def test_disable_expires_with_the_ttl(
    monkeypatch, secondary_categories_table, product_list
):
    # given a frozen clock and a feature disabled by a failed statement
    clock = {"now": 1000.0}
    monkeypatch.setattr(wsm_models.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(wsm_models, "PROBE_TTL_SECONDS", 60)
    clear_secondary_categories_cache()
    alias = settings.DATABASE_CONNECTION_DEFAULT_NAME
    add_secondary_category(product_list[0], Category.objects.first())
    assert secondary_categories_available(alias) is True
    wsm_models.disable_secondary_categories(alias)
    assert secondary_categories_available(alias) is False

    # when one interval passes
    clock["now"] += wsm_models.PROBE_TTL_SECONDS + 1

    # then the feature comes back rather than being pinned off
    assert secondary_categories_available(alias) is True


def test_collection_sentinel_aggregate_is_scoped_to_the_tree(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    published_collection,
):
    # given the COLLECTION sort, whose sentinel is an aggregate
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    published_collection.products.add(guest)

    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        query_category_products(
            user_api_client,
            category,
            channel_USD,
            first=20,
            sortBy={"field": "COLLECTION", "direction": "ASC"},
        )

    # then every sentinel aggregate is restricted to the requested tree, which is
    # upstream's cost class, rather than running over the whole visible catalog
    aggregates = [
        query["sql"]
        for query in ctx.captured_queries
        if 'MIN("product_collectionproduct"."sort_order")' in query["sql"]
    ]
    assert aggregates, "no collection sentinel aggregate was emitted"
    assert all(TREE_MEMBERSHIP_SQL in sql for sql in aggregates), aggregates
    # ... one per leg, since minima combine
    assert len(aggregates) == 2, aggregates


def test_aggregate_refuses_anything_but_a_minimum(product_list):
    # given the stand-in, whose only legitimate aggregate caller wants a Min
    from ..secondary_categories import MultiLegQuerySet

    legs = MultiLegQuerySet(Category.objects.all(), [Category.objects.all()])

    # then Min combines across the legs
    assert legs.aggregate(low=Min("pk")) == {
        "low": Category.objects.aggregate(low=Min("pk"))["low"]
    }
    # ... and anything that does not combine raises rather than returning a
    # plausible wrong number
    with pytest.raises(NotImplementedError):
        legs.aggregate(total=Count("pk"))
    with pytest.raises(NotImplementedError):
        legs.aggregate()


def test_probe_costs_one_statement_when_the_table_is_absent(
    user_api_client, product_list, channel_USD, real_probe_ttl
):
    # given no table at all, the state of every tenant that never ran PartsLogic
    category = Category.objects.first()
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        query_category_products(user_api_client, category, channel_USD, first=20)

    # then the probe short-circuits after the catalog lookup
    assert probe_statements_in(ctx) == [
        "SELECT to_regclass('product_secondary_categories')::oid"
    ]


def test_probe_costs_three_statements_when_the_table_is_empty(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given the table present and empty, which the fleet census says is the state
    # of Truck Outlaw, Body Kits, Steve White, Shifted, Caltric and Camlocker
    category = Category.objects.first()
    clear_secondary_categories_cache()
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        query_category_products(user_api_client, category, channel_USD, first=20)

    # then all three checks run, once per interval per alias. Pinned because
    # FORK-NOTES quotes this number.
    statements = probe_statements_in(ctx)
    assert len(statements) == 3, statements
    assert "has_table_privilege" in statements[1]
    assert "EXISTS (SELECT 1 FROM product_secondary_categories)" in statements[2]


WALK_SORTS = [
    None,
    {"field": "NAME", "direction": "ASC"},
    {"field": "NAME", "direction": "DESC"},
    {"field": "PRICE", "direction": "ASC"},
    {"field": "MINIMAL_PRICE", "direction": "DESC"},
    {"field": "PUBLISHED_AT", "direction": "ASC"},
    {"field": "RATING", "direction": "DESC"},
    {"field": "CREATED_AT", "direction": "ASC"},
    {"field": "TYPE", "direction": "ASC"},
    {"field": "LAST_MODIFIED_AT", "direction": "DESC"},
]


@pytest.mark.parametrize("sort_by", WALK_SORTS)
@pytest.mark.parametrize("page_size", [1, 2])
def test_full_walk_matches_the_unpaginated_union(
    sort_by,
    page_size,
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
):
    # given a secondary-only product, a product in BOTH legs, and a product
    # carrying two secondary rows inside the same tree
    category = Category.objects.first()
    child = Category.objects.create(name="Child", slug="child-walk", parent=category)
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    add_secondary_category(product_list[1], category)
    add_secondary_category(guest, child)
    extra = {} if sort_by is None else {"sortBy": sort_by}

    # when the whole connection is read in one page, then walked in both
    # directions at a page size that straddles the leg boundary
    whole = query_category_products(
        user_api_client, category, channel_USD, first=20, **extra
    )
    expected = [edge["node"]["slug"] for edge in whole["edges"]]

    # then no duplicates, a matching count, and both walks reproduce the order
    assert sorted(expected) == sorted(set(expected)), expected
    assert whole["totalCount"] == len(expected)
    assert (
        walk_every_page(user_api_client, category, channel_USD, page_size, **extra)
        == expected
    )
    assert (
        walk_every_page_backward(
            user_api_client, category, channel_USD, page_size, **extra
        )
        == expected
    )


def test_full_walk_by_attribute_matches_the_unpaginated_union(
    user_api_client, product_list, channel_USD, secondary_categories_table, product_type
):
    # given attribute sorting, which makes both legs GROUP BY queries
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    sort_by = {
        "attributeId": graphene.Node.to_global_id(
            "Attribute", product_type.product_attributes.first().pk
        ),
        "direction": "ASC",
    }

    # when read whole and then walked one product at a time
    whole = query_category_products(
        user_api_client, category, channel_USD, first=20, sortBy=sort_by
    )
    expected = [edge["node"]["slug"] for edge in whole["edges"]]

    # then the walk reproduces it exactly
    assert whole["totalCount"] == len(expected)
    assert (
        walk_every_page(user_api_client, category, channel_USD, 1, sortBy=sort_by)
        == expected
    )


def test_total_count_equals_the_distinct_walk_on_a_two_level_tree(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    categories_tree,
):
    # given a parent/child tree where one product has secondary rows on both
    # levels and another sits in both legs
    parent = categories_tree
    child = parent.children.first()
    guest = product_list[0]
    move_to_own_category(guest, "outside-the-tree")
    add_secondary_category(guest, parent)
    add_secondary_category(guest, child)
    add_secondary_category(product_list[1], child)
    product_list[1].category = child
    product_list[1].save(update_fields=["category"])

    # when the ancestor is read and then walked
    data = query_category_products(user_api_client, parent, channel_USD, first=20)
    walked = walk_every_page(user_api_client, parent, channel_USD, 1)

    # then nothing is double counted
    assert sorted(walked) == sorted(set(walked)), walked
    assert data["totalCount"] == len(set(walked))


# ---------------------------------------------------------------------------
# A fourth cold adversarial review (round 4, at 8f57f19) found that the
# correctness core of the whole two-leg shape had no binding coverage: every
# fixture above holds three products, so the per-leg LIMIT never binds and
# deleting the per-leg ORDER BY left all 109 tests green. It also found that the
# count path guarded a statement that never names the secondary table, and that
# the "unrelated error" probes above all carry no SQLSTATE, so they were passing
# on the evidence layer alone and the structural layer was unpinned. Its probes
# are folded in below, the two that asserted defects inverted.
# ---------------------------------------------------------------------------

LEG_SIZE = 8


@pytest.fixture
def big_two_leg_tree(db, product_type, channel_USD):
    """Build 8 primary and 8 secondary-only products, in reverse sort order.

    Reverse insertion order is the point: with a page smaller than either leg, an
    unordered per-leg LIMIT picks the physically-first rows, which are the
    globally LAST ones, so any loss of the per-leg ordering shows up as a wrong
    page rather than as nothing.
    """
    tree = Category.objects.create(name="Tree", slug="r4-tree")
    outside = Category.objects.create(name="Outside", slug="r4-outside")
    with connection.cursor() as cursor:
        cursor.execute(SECONDARY_CATEGORIES_DDL)

    made = []
    for index in range(LEG_SIZE * 2 - 1, -1, -1):
        primary = index % 2 == 0
        product = Product.objects.create(
            name=f"R4 product {index:02d}",
            slug=f"r4-p-{index:02d}",
            product_type=product_type,
            category=tree if primary else outside,
        )
        ProductChannelListing.objects.create(
            product=product,
            channel=channel_USD,
            is_published=True,
            visible_in_listings=True,
            currency=channel_USD.currency_code,
            discounted_price_amount=LEG_SIZE * 2 - index,
            available_for_purchase_at=datetime.datetime(
                1999, 1, 1, tzinfo=datetime.UTC
            ),
        )
        # A priced variant per product, so PRICE and MINIMAL_PRICE order by real
        # values here. Without one, `min_variants_price_amount` is NULL for every
        # row and any assertion about PRICE ordering is vacuous, which a cold
        # review pointed out about the first version of this fixture.
        variant = ProductVariant.objects.create(
            product=product, sku=f"r4-sku-{index:02d}"
        )
        # Prices run ANTI-CORRELATED with slug and name. PRICE's tiebreaks are
        # ["min_variants_price_amount", "name", "slug"], so with price ascending
        # alongside slug a completely broken price annotation still produces the
        # right page through the tiebreak alone, which a cold review proved by
        # neutering the annotation with every test still green. Reversed, the
        # assertion can only pass if the price is actually being read.
        ProductVariantChannelListing.objects.create(
            variant=variant,
            channel=channel_USD,
            price_amount=LEG_SIZE * 2 - index,
            discounted_price_amount=LEG_SIZE * 2 - index,
            currency=channel_USD.currency_code,
        )
        if not primary:
            add_secondary_category(product, tree)
        made.append(product)
    return tree, sorted(made, key=lambda product: product.slug)


@pytest.fixture
def staff_reader(staff_api_client, permission_manage_products):
    staff_api_client.user.user_permissions.add(permission_manage_products)
    return staff_api_client


def test_per_leg_limit_returns_the_global_top_k_not_a_leg_local_one(
    staff_reader, big_two_leg_tree, channel_USD
):
    # given a page far smaller than either leg
    tree, ordered = big_two_leg_tree

    # when the first three are requested in the default (slug) ordering
    slugs = query_category_product_slugs(staff_reader, tree, channel_USD, first=3)

    # then they are the three globally first, not the first three of one leg and
    # not the physically-first rows
    assert slugs == [product.slug for product in ordered[:3]], slugs


def test_per_leg_limit_under_an_aggregate_ordering(
    staff_reader, big_two_leg_tree, channel_USD
):
    # given MINIMAL_PRICE, whose per-leg subquery groups and orders by an
    # aggregate that `values("pk")` masks out of the select list: the one shape
    # where the per-leg ordering could be lost silently
    tree, ordered = big_two_leg_tree
    # prices are anti-correlated with slug order, so cheapest-first is the REVERSE
    # of slug order: an assertion the tiebreak cannot satisfy by accident
    cheapest = sorted(ordered, key=lambda product: -int(product.slug.rsplit("-", 1)[1]))

    slugs = query_category_product_slugs(
        staff_reader,
        tree,
        channel_USD,
        first=3,
        sortBy={"field": "MINIMAL_PRICE", "direction": "ASC"},
    )

    # then the ordering survived into the union arms
    assert slugs == [product.slug for product in cheapest[:3]], slugs


@pytest.mark.parametrize("page_size", [1, 3, 5])
def test_full_walk_at_scale_matches_the_unpaginated_union(
    page_size, staff_reader, big_two_leg_tree, channel_USD
):
    # given a walk where every page makes the per-leg LIMIT bite
    tree, ordered = big_two_leg_tree
    expected = [product.slug for product in ordered]

    whole = query_category_products(staff_reader, tree, channel_USD, first=100)

    # then one page, a forward walk and a backward walk all agree
    assert [edge["node"]["slug"] for edge in whole["edges"]] == expected
    assert whole["totalCount"] == len(expected)
    assert walk_every_page(staff_reader, tree, channel_USD, page_size) == expected
    assert (
        walk_every_page_backward(staff_reader, tree, channel_USD, page_size) == expected
    )


@pytest.mark.parametrize(
    "sort_by",
    [
        {"field": "MINIMAL_PRICE", "direction": "ASC"},
        {"field": "MINIMAL_PRICE", "direction": "DESC"},
        {"field": "NAME", "direction": "DESC"},
    ],
)
def test_full_walk_at_scale_under_sorting(
    sort_by, staff_reader, big_two_leg_tree, channel_USD
):
    # given a sort that interleaves the legs, at binding scale
    tree, _ordered = big_two_leg_tree
    whole = query_category_products(
        staff_reader, tree, channel_USD, first=100, sortBy=sort_by
    )
    expected = [edge["node"]["slug"] for edge in whole["edges"]]

    # then both walks reproduce the single-page order exactly
    assert len(expected) == LEG_SIZE * 2
    assert sorted(expected) == sorted(set(expected))
    assert (
        walk_every_page(staff_reader, tree, channel_USD, 3, sortBy=sort_by) == expected
    )
    assert (
        walk_every_page_backward(staff_reader, tree, channel_USD, 3, sortBy=sort_by)
        == expected
    )


def test_last_only_at_scale_returns_the_global_tail(
    staff_reader, big_two_leg_tree, channel_USD
):
    # given `last` with no cursor, which reverses the ordering inside every leg
    tree, ordered = big_two_leg_tree

    data = query_category_products(staff_reader, tree, channel_USD, last=3)

    # then the tail is the global tail, not a per-leg one
    assert [edge["node"]["slug"] for edge in data["edges"]] == [
        product.slug for product in ordered[-3:]
    ]


def test_the_table_vanishing_mid_walk_does_not_error(
    staff_reader, big_two_leg_tree, channel_USD, real_probe_ttl
):
    # given a walk begun while the union was available, at binding scale
    tree, _ordered = big_two_leg_tree
    first_page = query_category_products(staff_reader, tree, channel_USD, first=3)
    assert first_page["totalCount"] == LEG_SIZE * 2
    cursor_value = first_page["pageInfo"]["endCursor"]

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # when page two is requested with a cursor minted by the union
    body = raw_category_products(
        staff_reader, tree, channel=channel_USD.slug, first=3, after=cursor_value
    )

    # then it is answered primary-only rather than raising
    assert "errors" not in body, body["errors"][0]["message"]
    slugs = [
        edge["node"]["slug"] for edge in body["data"]["category"]["products"]["edges"]
    ]
    assert slugs, body["data"]
    assert all(int(slug.rsplit("-", 1)[1]) % 2 == 0 for slug in slugs), slugs


def test_slicing_rejects_what_it_cannot_honour(big_two_leg_tree):
    # given the stand-in driven directly, since upstream never sends these
    from ..secondary_categories import MultiLegQuerySet

    tree, ordered = big_two_leg_tree
    base = Product.objects.filter(slug__startswith="r4-p-").order_by("slug")
    legs = MultiLegQuerySet(
        base, [base.filter(category=tree), base.exclude(category=tree)]
    )

    # then a whole-page slice is answered, and correctly
    assert [product.slug for product in legs[:4]] == [
        product.slug for product in ordered[:4]
    ]
    # ... an offset slice raises, because pushing it per-leg would return the
    # union of each leg's own rows 4..8 and look entirely plausible
    with pytest.raises(NotImplementedError, match="OFFSET"):
        legs[4:8]
    # ... and an index raises the same way instead of an AttributeError about
    # dicts from three frames down
    with pytest.raises(NotImplementedError, match="not indexing"):
        legs[0]
    # ... and so does a step, which Django answers with a plain `list` rather
    # than a queryset, so the union three frames down would raise AttributeError
    with pytest.raises(NotImplementedError, match="step"):
        legs[0:4:2]


def test_upstream_only_ever_slices_from_zero(
    user_api_client, product_list, channel_USD, secondary_categories_table
):
    # given the claim the guard above rests on: `connection_from_queryset_slice`
    # slices `[:end_margin]` and never with an offset. Pinned by observation
    # rather than by reading, so an upstream change to offset pagination fails
    # here instead of returning wrong pages.
    from ..secondary_categories import MultiLegQuerySet

    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    seen = []
    original = MultiLegQuerySet.__getitem__

    def recording_getitem(self, key):
        seen.append(key)
        return original(self, key)

    MultiLegQuerySet.__getitem__ = recording_getitem
    try:
        query_category_products(user_api_client, category, channel_USD, first=2)
        query_category_products(user_api_client, category, channel_USD, last=2)
    finally:
        MultiLegQuerySet.__getitem__ = original

    assert seen, "the connection never sliced the stand-in"
    assert all(isinstance(key, slice) for key in seen), seen
    assert all(key.start in (None, 0) for key in seen), seen
    # ... and never with a step either. Django returns a plain `list` for a
    # strided slice, so an upstream move to strided slicing would have died as
    # `AttributeError: 'list' object has no attribute 'union'` three frames down
    # rather than on the guard, which is the complaint that produced the guard.
    assert all(key.step is None for key in seen), seen


def test_a_real_undefined_table_from_outside_the_guard_surfaces(
    staff_reader,
    product_list,
    channel_USD,
    secondary_categories_table,
    published_collection,
    real_probe_ttl,
):
    # given a REAL Postgres 42P01 (an accepted SQLSTATE) from a statement that
    # runs before the guard: the COLLECTION sentinel aggregate. This is what pins
    # the structural layer; the probes further up carry no SQLSTATE and so are
    # rejected by the evidence layer before structure matters.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    published_collection.products.add(guest, product_list[1])
    sort_by = {"field": "COLLECTION", "direction": "ASC"}
    assert "errors" not in raw_category_products(
        staff_reader, category, channel=channel_USD.slug, first=20, sortBy=sort_by
    )
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE product_collectionproduct RENAME TO r4_gone_cp")

    # when the same request runs
    body = raw_category_products(
        staff_reader, category, channel=channel_USD.slug, first=20, sortBy=sort_by
    )

    # then it errors rather than becoming a quietly truncated 200
    assert "errors" in body, f"a real 42P01 was swallowed: {body['data']}"


def test_a_real_undefined_table_shared_with_the_fallback_surfaces(
    staff_reader,
    product_list,
    channel_USD,
    secondary_categories_table,
    published_collection,
    real_probe_ttl,
):
    # given a REAL 42P01 from a table named INSIDE the guarded statement, so
    # neither structure nor evidence can separate it and only corroboration can
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    published_collection.products.add(guest, product_list[1])
    collection_filter = {
        "collections": [
            graphene.Node.to_global_id("Collection", published_collection.pk)
        ]
    }
    warm = raw_category_products(
        staff_reader,
        category,
        channel=channel_USD.slug,
        first=20,
        filter=collection_filter,
    )
    assert "errors" not in warm, warm["errors"]
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE product_collectionproduct RENAME TO r4_gone_cp2")

    body = raw_category_products(
        staff_reader,
        category,
        channel=channel_USD.slug,
        first=20,
        filter=collection_filter,
    )

    assert "errors" in body, f"a real 42P01 was swallowed: {body['data']}"


def test_a_real_permission_denied_shared_with_the_fallback_surfaces(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a REAL 42501 on a table the visibility chain needs, which both the
    # union statement and the primary-only fallback name
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    assert (
        query_category_products(user_api_client, category, channel_USD, first=20)[
            "totalCount"
        ]
        == 3
    )
    with connection.cursor() as cursor:
        cursor.execute("CREATE ROLE r4_partial_read")
        cursor.execute("GRANT r4_partial_read TO CURRENT_USER")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO r4_partial_read")
        cursor.execute(
            "REVOKE SELECT ON product_productvariantchannellisting FROM r4_partial_read"
        )
        # SET LOCAL so the test transaction's rollback restores the role; an
        # aborted block cannot run RESET
        cursor.execute("SET LOCAL ROLE r4_partial_read")

    body = raw_category_products(
        user_api_client, category, channel=channel_USD.slug, first=20
    )

    assert "errors" in body, f"a real 42501 was swallowed: {body['data']}"


@pytest.mark.parametrize("sqlstate", ["57014", "42601", "22P02", "40001"])
def test_a_sqlstate_outside_the_verdict_set_surfaces(
    sqlstate,
    staff_reader,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a driver verdict that is not one this code accepts: a statement
    # timeout, a syntax error, a bad cast, a serialization failure. None of them
    # says the table is unreadable. (42883 IS accepted now, because a
    # column-type change is a table-shape verdict; they are pinned as recognised
    # in test_the_real_verdicts_on_our_table_are_still_recognised.)
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    with fail_statement(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        "= ANY (ARRAY(",
        driver_error(ProgrammingError("wrapped"), sqlstate),
    ) as state:
        body = raw_category_products(
            staff_reader, category, channel=channel_USD.slug, first=20
        )

    assert state["fired"], "the secondary leg never ran"
    assert "errors" in body, f"{sqlstate} was swallowed: {body['data']}"


def test_a_transient_failure_of_the_primary_count_surfaces(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given the primary-leg count failing once with an ACCEPTED SQLSTATE. It is
    # upstream's own statement and it never names the secondary table, so it must
    # not be answered as "the secondary table is unreadable": that produced a
    # silently wrong totalCount at 200, a log blaming the wrong table, and the
    # union disabled for a whole TTL.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel_USD.slug,
    }
    assert (
        get_graphql_content(
            user_api_client.post_graphql(
                QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
            )
        )["data"]["category"]["products"]["totalCount"]
        == 3
    )

    with fail_statement_where(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        lambda sql: "COUNT(*)" in sql and "= ANY (ARRAY(" not in sql,
        driver_error(
            ProgrammingError('relation "something_else" does not exist'), "42P01"
        ),
    ) as state:
        body = user_api_client.post_graphql(
            QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
        ).json()

    # then it surfaces, and the union is left on
    assert state["fired"], "the primary leg count never ran"
    assert "errors" in body, f"a primary-count failure was swallowed: {body['data']}"
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )


def test_degrading_logs_a_warning_naming_the_table(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
    caplog,
):
    # given a degradation, whose only trace anywhere in the stack is this log:
    # the fallback compiles different SQL and succeeds, so nothing else reports it
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    query_category_products(user_api_client, category, channel_USD, first=20)
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    with caplog.at_level(logging.WARNING):
        assert (
            query_category_products(user_api_client, category, channel_USD, first=20)[
                "totalCount"
            ]
            == 2
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and SECONDARY_CATEGORIES_TABLE in record.getMessage()
    ]
    assert warnings, [record.getMessage() for record in caplog.records]


def test_probe_costs_three_statements_inside_a_transaction_block(
    staff_reader, product_list, channel_USD, real_probe_ttl
):
    # given no table and an open transaction block, which is what a
    # `Category.products` edges selection inside a mutation payload runs in.
    # `recoverable_failure` adds a SAVEPOINT/RELEASE pair there, so the probe
    # costs three statements rather than the one it costs in autocommit.
    # FORK-NOTES quotes both numbers; this pins the second.
    category = Category.objects.first()
    clear_secondary_categories_cache()
    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        staff_reader.post_graphql(
            CATEGORY_UPDATE_WITH_PRODUCTS,
            {
                "id": graphene.Node.to_global_id("Category", category.pk),
                "name": "Probe cost",
                "channel": channel_USD.slug,
            },
        )

    statements = [query["sql"] for query in ctx.captured_queries]
    probe = [sql for sql in statements if "to_regclass" in sql]
    savepoints = [
        sql for sql in statements if sql.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
    ]
    assert len(probe) == 1, probe
    assert len(savepoints) == 2, savepoints


def test_filter_by_category_widens_with_the_union(
    staff_reader, product_list, channel_USD, secondary_categories_table
):
    # given `filter: {categories: [X]}` where X is OUTSIDE the queried tree.
    # Upstream intersected it with the tree and returned nothing; with the union
    # on, a product primary in X that reaches the tree through a secondary row is
    # returned. Documented in FORK-NOTES as a deliberate widening rather than
    # left as a surprise for whoever hits it in the dashboard.
    category = Category.objects.first()
    guest = product_list[0]
    outside = move_to_own_category(guest, "somewhere-else")

    upstream_answer = query_category_product_slugs(
        staff_reader,
        category,
        channel_USD,
        first=20,
        filter={"categories": [graphene.Node.to_global_id("Category", outside.pk)]},
    )
    assert upstream_answer == []

    add_secondary_category(guest, category)
    clear_secondary_categories_cache()
    slugs = query_category_product_slugs(
        staff_reader,
        category,
        channel_USD,
        first=20,
        filter={"categories": [graphene.Node.to_global_id("Category", outside.pk)]},
    )

    assert slugs == [guest.slug], slugs


def test_collection_sort_row_multiplication_matches_upstream(
    staff_reader, product_type, channel_USD, db
):
    # given six products each in three collections. `sortBy: COLLECTION` is the
    # one ordering whose annotation is not an aggregate, so Django adds no
    # GROUP BY and a product appears once per collection. That is upstream's
    # behaviour, and the per-leg LIMIT counting join rows is therefore parity
    # rather than a fork defect; this pins both halves of that claim.
    tree = Category.objects.create(name="Coll tree", slug="r4-coll-tree")
    collections = [
        Collection.objects.create(name=f"C{index}", slug=f"r4-c-{index}")
        for index in range(3)
    ]
    products = []
    for index in range(6):
        product = Product.objects.create(
            name=f"Coll {index}",
            slug=f"r4-coll-{index}",
            product_type=product_type,
            category=tree,
        )
        ProductChannelListing.objects.create(
            product=product,
            channel=channel_USD,
            is_published=True,
            visible_in_listings=True,
            currency=channel_USD.currency_code,
            discounted_price_amount=index + 1,
        )
        for order, collection in enumerate(collections):
            CollectionProduct.objects.create(
                collection=collection, product=product, sort_order=index * 3 + order
            )
        products.append(product)
    sort_by = {"field": "COLLECTION", "direction": "ASC"}

    # upstream's answer, with no table at all
    upstream = walk_every_page(staff_reader, tree, channel_USD, 3, sortBy=sort_by)

    with connection.cursor() as cursor:
        cursor.execute(SECONDARY_CATEGORIES_DDL)
    guest = Product.objects.create(
        name="Coll guest",
        slug="r4-coll-guest",
        product_type=product_type,
        category=Category.objects.create(name="Elsewhere", slug="r4-coll-elsewhere"),
    )
    ProductChannelListing.objects.create(
        product=guest,
        channel=channel_USD,
        is_published=True,
        visible_in_listings=True,
        currency=channel_USD.currency_code,
        discounted_price_amount=99,
    )
    add_secondary_category(guest, tree)
    clear_secondary_categories_cache()
    patched = walk_every_page(staff_reader, tree, channel_USD, 3, sortBy=sort_by)

    # then upstream already repeats a product once per collection, so the fork's
    # join-row bound is parity...
    assert len(upstream) > len({*upstream}), upstream
    # ... and nothing upstream reached has gone missing
    assert {*upstream} == {product.slug for product in products}
    assert {*upstream} <= {*patched}, sorted({*upstream} - {*patched})
    assert guest.slug in patched


def test_two_categories_in_one_request_after_a_degradation(
    staff_reader, product_list, channel_USD, secondary_categories_table, real_probe_ttl
):
    # given one document selecting two categories, both with a secondary member
    first_category = Category.objects.first()
    second_category = Category.objects.create(name="Second", slug="r4-second")
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, first_category)
    add_secondary_category(guest, second_category)
    variables = {
        "a": graphene.Node.to_global_id("Category", first_category.pk),
        "b": graphene.Node.to_global_id("Category", second_category.pk),
        "channel": channel_USD.slug,
    }
    warm = get_graphql_content(
        staff_reader.post_graphql(TWO_CATEGORIES, variables=variables)
    )["data"]
    assert warm["first"]["products"]["totalCount"] == 3
    assert warm["second"]["products"]["totalCount"] == 1

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # when both are resolved after the table has gone
    body = staff_reader.post_graphql(TWO_CATEGORIES, variables=variables).json()

    # then both degrade in the same response, rather than the second raising
    # after the first flipped the probe
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["first"]["products"]["totalCount"] == 2
    assert body["data"]["second"]["products"]["totalCount"] == 0


def test_where_with_the_or_operator_reaches_both_legs(
    staff_reader, product_list, channel_USD, secondary_categories_table
):
    # given `where` with OR, where only AND was covered above
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)

    slugs = query_category_product_slugs(
        staff_reader,
        category,
        channel_USD,
        first=20,
        where={
            "OR": [
                {"slug": {"eq": guest.slug}},
                {"slug": {"eq": product_list[1].slug}},
            ]
        },
    )

    # then it applies across the leg boundary
    assert sorted(slugs) == sorted([guest.slug, product_list[1].slug]), slugs


@contextmanager
def statements_inside_the_guard():
    """Record every statement that executes inside the degradation guard.

    The structural layer is a property of WHICH statements the guard covers, so
    asserting it end-to-end is unreliable: widening the guard usually still
    produces an error further down, which makes a symptom-based test pass for the
    wrong reason. A cold review found exactly that, having reverted the layer two
    ways with every test still green. This observes the property directly.
    """
    from .. import secondary_categories

    recorded: list[str] = []
    original = secondary_categories.recoverable_failure

    @contextmanager
    def recording(alias):
        def wrapper(execute, sql, params, many, context):
            recorded.append(sql)
            return execute(sql, params, many, context)

        with connections[alias].execute_wrapper(wrapper), original(alias):
            yield

    secondary_categories.recoverable_failure = recording
    try:
        yield recorded
    finally:
        secondary_categories.recoverable_failure = original


@contextmanager
def every_statement_on_every_alias():
    """Record every statement on every alias, in the same text form as the guard.

    Deliberately an `execute_wrapper` rather than `CaptureQueriesContext`: the
    latter reports the interpolated statement while the guard's recorder reports
    the parameterised one, so comparing the two sets by equality would silently
    never match and the converse assertion below would pass vacuously.
    """
    recorded: list[str] = []

    def wrapper(execute, sql, params, many, context):
        recorded.append(sql)
        return execute(sql, params, many, context)

    with (
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME].execute_wrapper(wrapper),
        connections[settings.DATABASE_CONNECTION_REPLICA_NAME].execute_wrapper(wrapper),
    ):
        yield recorded


def test_every_statement_naming_the_table_is_inside_the_guard(
    staff_reader,
    product_list,
    channel_USD,
    secondary_categories_table,
    published_collection,
):
    # given the sort with the widest set of surrounding statements: COLLECTION
    # runs a sentinel aggregate PER LEG before any slice is taken, so it is the
    # shape most likely to leave a psc-naming statement outside the guard. It did,
    # for three rounds, and that was a 500 on a genuine table loss.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    published_collection.products.add(guest, product_list[1])

    with (
        every_statement_on_every_alias() as everything,
        statements_inside_the_guard() as guarded,
    ):
        query_category_products(
            staff_reader,
            category,
            channel_USD,
            first=20,
            sortBy={"field": "COLLECTION", "direction": "ASC"},
        )

    assert guarded, "no statement executed inside the guard"

    # then nothing UNRELATED is inside the guard, which is what keeps an unrelated
    # failure out of scope...
    unrelated = [
        sql
        for sql in guarded
        if SECONDARY_CATEGORIES_TABLE not in sql
        and not sql.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
    ]
    assert not unrelated, unrelated

    # ... and, the converse and the one that was missing, which is the assertion
    # this test exists for: every statement executed ANYWHERE in the request that
    # names the table is inside the guard. The forward direction alone let the
    # `COLLECTION` sentinel aggregate sit outside it for three rounds, naming the
    # table, and a genuine table loss on that one sort was a hard 500 repeating
    # for the rest of the TTL. Both directions, or the next one hides the same way.
    # The probe is the one exception, and it is pinned LITERALLY rather than by a
    # pattern, so a new psc-naming statement cannot hide behind a loose match. It
    # carries its own broad `except DatabaseError` instead of the guard.
    probe_statements = {
        f"SELECT to_regclass('{SECONDARY_CATEGORIES_TABLE}')::oid",
        f"SELECT EXISTS (SELECT 1 FROM {SECONDARY_CATEGORIES_TABLE})",
    }
    naming = [sql for sql in everything if SECONDARY_CATEGORIES_TABLE in sql]
    assert naming, "no statement named the table at all"
    outside = [
        sql for sql in naming if sql not in guarded and sql not in probe_statements
    ]
    assert not outside, outside


# ---------------------------------------------------------------------------
# A fifth cold adversarial review (round 5, at 4d01ee8) found that the round-4
# fix had moved the problem rather than removed it: the count path's fallback was
# `lambda: 0`, which executes nothing, so the corroboration layer did not exist
# there while the docstring, FORK-NOTES and the PR body all said it did. It also
# found the binding-scale fixture left PRICE ordering vacuous, three semantic
# widenings documented as one, and several bite-check counts in the PR body that
# did not reproduce. Its probes are folded in below.
# ---------------------------------------------------------------------------


def is_secondary_count(sql):
    return "COUNT(*)" in sql and "= ANY (ARRAY(" in sql


def test_a_secondary_count_failure_naming_another_relation_surfaces(
    staff_reader, big_two_leg_tree, channel_USD, real_probe_ttl, caplog
):
    # given the secondary-leg COUNT failing with an accepted SQLSTATE whose message
    # names a completely different relation. Structure cannot help (the statement
    # really does name the secondary table) and corroboration cannot either (the
    # fault is one-shot), so the relation name in the message is the only evidence
    # there is, and it says this is not about us.
    tree, _ordered = big_two_leg_tree
    variables = {
        "id": graphene.Node.to_global_id("Category", tree.pk),
        "channel": channel_USD.slug,
    }

    with (
        caplog.at_level(logging.WARNING),
        fail_statement_where(
            settings.DATABASE_CONNECTION_DEFAULT_NAME,
            is_secondary_count,
            driver_error(
                ProgrammingError('relation "some_unrelated_table" does not exist'),
                "42P01",
            ),
        ) as state,
    ):
        body = staff_reader.post_graphql(
            QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
        ).json()

    # then it surfaces instead of answering with a primary-only count at 200...
    assert state["fired"], "the secondary count never ran"
    assert "errors" in body, f"an unrelated 42P01 was swallowed: {body['data']}"
    # ... the union is left on...
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )
    # ... and nothing was logged blaming our table for someone else's problem
    assert not [
        record
        for record in caplog.records
        if SECONDARY_CATEGORIES_TABLE in record.getMessage()
        and "some_unrelated_table" in record.getMessage()
    ]


def test_a_secondary_count_failure_shared_with_the_corroborator_surfaces(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a fault that is real and shared: a column both counts name is gone, so
    # the SQLSTATE is 42703, whose message names a column rather than a relation
    # and therefore cannot be matched against our table name. Only the
    # corroborating fallback can separate this from the secondary table breaking,
    # and the count path had no corroborator until this was fixed.
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel_USD.slug,
    }
    assert (
        get_graphql_content(
            user_api_client.post_graphql(
                QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
            )
        )["data"]["category"]["products"]["totalCount"]
        == 3
    )
    fired = {"count": 0}
    real_error = driver_error(ProgrammingError('column "gone" does not exist'), "42703")

    def wrapper(execute, sql, params, many, context):
        # every COUNT(*) fails, secondary and corroborator alike, which is what a
        # genuinely shared fault looks like
        if "COUNT(*)" in sql:
            fired["count"] += 1
            raise real_error
        return execute(sql, params, many, context)

    with connections[settings.DATABASE_CONNECTION_DEFAULT_NAME].execute_wrapper(
        wrapper
    ):
        body = user_api_client.post_graphql(
            QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
        ).json()

    # then it surfaces, because the corroborator failed the same way
    assert fired["count"] >= 1
    assert "errors" in body, f"a shared 42703 was swallowed: {body['data']}"
    assert (
        secondary_categories_available(settings.DATABASE_CONNECTION_REPLICA_NAME)
        is True
    )


def test_the_count_path_corroborates_before_it_degrades(
    user_api_client,
    product_list,
    channel_USD,
    secondary_categories_table,
    real_probe_ttl,
):
    # given a genuine loss of the table, where degrading IS the right answer
    category = Category.objects.first()
    guest = product_list[0]
    move_to_own_category(guest, "somewhere-else")
    add_secondary_category(guest, category)
    variables = {
        "id": graphene.Node.to_global_id("Category", category.pk),
        "channel": channel_USD.slug,
    }
    assert (
        get_graphql_content(
            user_api_client.post_graphql(
                QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
            )
        )["data"]["category"]["products"]["totalCount"]
        == 3
    )
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    with CaptureQueriesContext(
        connections[settings.DATABASE_CONNECTION_DEFAULT_NAME]
    ) as ctx:
        body = user_api_client.post_graphql(
            QUERY_CATEGORY_TOTAL_COUNT_ONLY, variables=variables
        ).json()

    # then it degrades...
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == 2
    # ... and it ran a corroborating statement to get there, rather than assuming.
    # Two unfenced counts: the primary one, and the corroborator re-running it.
    counts = [
        query["sql"]
        for query in ctx.captured_queries
        if "COUNT(*)" in query["sql"] and "= ANY (ARRAY(" not in query["sql"]
    ]
    assert len(counts) == 2, counts


def test_total_count_can_be_smaller_than_the_edges_in_one_response(
    staff_reader, big_two_leg_tree, channel_USD, real_probe_ttl
):
    # given a table loss that lands between the page statement and the count
    # callable graphene resolves after the resolver has returned
    tree, _ordered = big_two_leg_tree

    with fail_statement_where(
        settings.DATABASE_CONNECTION_DEFAULT_NAME,
        is_secondary_count,
        driver_error(
            ProgrammingError('relation "product_secondary_categories" does not exist'),
            "42P01",
        ),
    ) as state:
        data = query_category_products(staff_reader, tree, channel_USD, first=100)

    # then ONE response body carries the union's edges beside a primary-only
    # count, so `totalCount` is strictly SMALLER than the edges handed over with
    # it. Documented rather than fixed: closing it needs atomicity across a
    # boundary upstream owns. Bounded by one TTL, since the next request skips the
    # leg entirely.
    assert state["fired"]
    assert len(data["edges"]) == LEG_SIZE * 2
    assert data["totalCount"] == LEG_SIZE
    assert data["totalCount"] < len(data["edges"])


def test_where_category_widens_with_the_union(
    staff_reader, product_list, channel_USD, secondary_categories_table
):
    # given `where: {category: {oneOf: [X]}}` where X is outside the queried tree.
    # Second of the three widenings, and the one the dashboard uses.
    category = Category.objects.first()
    guest = product_list[0]
    outside = move_to_own_category(guest, "somewhere-else")
    outside_id = graphene.Node.to_global_id("Category", outside.pk)

    assert (
        query_category_product_slugs(
            staff_reader,
            category,
            channel_USD,
            first=20,
            where={"category": {"oneOf": [outside_id]}},
        )
        == []
    )

    add_secondary_category(guest, category)
    clear_secondary_categories_cache()
    slugs = query_category_product_slugs(
        staff_reader,
        category,
        channel_USD,
        first=20,
        where={"category": {"oneOf": [outside_id]}},
    )

    assert slugs == [guest.slug], slugs


@pytest.mark.parametrize("argument", ["filter", "where"])
def test_has_category_false_widens_with_the_union(
    argument, staff_reader, product_list, channel_USD, secondary_categories_table
):
    # given `hasCategory: false`, which upstream made structurally empty on
    # `Category.products` because it restricted the base to the tree first. Third
    # of the three widenings, and arguably the feature becoming more correct: a
    # product with no primary category at all can now reach a category page.
    category = Category.objects.first()
    guest = product_list[0]
    guest.category = None
    guest.save(update_fields=["category"])

    assert (
        query_category_product_slugs(
            staff_reader,
            category,
            channel_USD,
            first=20,
            **{argument: {"hasCategory": False}},
        )
        == []
    )

    add_secondary_category(guest, category)
    clear_secondary_categories_cache()
    slugs = query_category_product_slugs(
        staff_reader,
        category,
        channel_USD,
        first=20,
        **{argument: {"hasCategory": False}},
    )

    assert slugs == [guest.slug], slugs


ALL_SORT_DIRECTIONS = [
    (field, direction)
    for field in ALL_PRODUCT_SORT_FIELDS
    if field != "RANK"
    for direction in ("ASC", "DESC")
]


@pytest.mark.parametrize(("field", "direction"), ALL_SORT_DIRECTIONS)
def test_every_sort_field_at_binding_scale(
    field, direction, staff_reader, big_two_leg_tree, channel_USD
):
    # given every sort the schema accepts, in both directions, at a size where the
    # per-leg LIMIT actually binds. The sweep further up runs all 14 at three
    # products, where it never binds, so it proves the fields are accepted rather
    # than that the merge is right for them.
    tree, _ordered = big_two_leg_tree
    sort_by = {"field": field, "direction": direction}

    whole = query_category_products(
        staff_reader, tree, channel_USD, first=100, sortBy=sort_by
    )
    expected = [edge["node"]["slug"] for edge in whole["edges"]]

    # then one page, a forward walk and a backward walk agree, with every product
    # exactly once
    assert len(expected) == LEG_SIZE * 2, (field, direction, expected)
    assert sorted(expected) == sorted(set(expected))
    assert whole["totalCount"] == LEG_SIZE * 2
    assert (
        walk_every_page(staff_reader, tree, channel_USD, 3, sortBy=sort_by) == expected
    ), (field, direction)
    assert (
        walk_every_page_backward(staff_reader, tree, channel_USD, 3, sortBy=sort_by)
        == expected
    ), (field, direction)


@pytest.mark.parametrize("page_size", [1, 3, 5, 7])
def test_customer_path_walk_at_binding_scale(
    page_size, user_api_client, big_two_leg_tree, channel_USD
):
    # given the storefront visibility chain rather than a staff read, at binding
    # scale. Every other binding-scale test here reads as staff, which skips
    # `visible_to_user`, the `visible_in_listings` annotation and its exclude, so
    # the shape the storefront actually runs was uncovered at scale.
    tree, ordered = big_two_leg_tree
    expected = [product.slug for product in ordered]

    whole = query_category_products(user_api_client, tree, channel_USD, first=100)

    assert [edge["node"]["slug"] for edge in whole["edges"]] == expected
    assert whole["totalCount"] == len(expected)
    assert walk_every_page(user_api_client, tree, channel_USD, page_size) == expected
    assert (
        walk_every_page_backward(user_api_client, tree, channel_USD, page_size)
        == expected
    )


@pytest.mark.parametrize("direction", ["ASC", "DESC"])
def test_attribute_sorting_at_binding_scale(
    direction, staff_reader, big_two_leg_tree, channel_USD, product_type, size_attribute
):
    # given attribute sorting at binding scale with the attribute value order the
    # exact REVERSE of slug order, so a lost per-leg ordering or a broken
    # StringAgg grouping under `values("pk")` shows up as a wrong page rather than
    # as a tie. This is also the ONE non-total sort order (its tiebreak is `name`,
    # which is not unique), so it is the only place the per-leg top-k argument
    # leans on the tiebreak.
    tree, ordered = big_two_leg_tree
    product_type.product_attributes.add(size_attribute)
    values = list(size_attribute.values.all())
    for index, product in enumerate(reversed(ordered)):
        associate_attribute_values_to_instance(
            product, {size_attribute.pk: [values[index % len(values)]]}
        )
    sort_by = {
        "attributeId": graphene.Node.to_global_id("Attribute", size_attribute.pk),
        "direction": direction,
    }

    whole = query_category_products(
        staff_reader, tree, channel_USD, first=100, sortBy=sort_by
    )
    expected = [edge["node"]["slug"] for edge in whole["edges"]]

    assert len(expected) == LEG_SIZE * 2
    assert sorted(expected) == sorted(set(expected))
    assert (
        walk_every_page(staff_reader, tree, channel_USD, 3, sortBy=sort_by) == expected
    )


def test_collection_sort_at_binding_scale_keeps_every_product(
    staff_reader, big_two_leg_tree, channel_USD
):
    # given every product in three collections at binding scale, so the per-leg
    # LIMIT counts join rows and bites three times harder than the product count
    # suggests. The parity test further up uses 6 products and asserts only
    # containment; this asserts nothing is lost when the join multiplies at scale.
    tree, ordered = big_two_leg_tree
    collections = [
        Collection.objects.create(name=f"BC{index}", slug=f"r5-bc-{index}")
        for index in range(3)
    ]
    for index, product in enumerate(ordered):
        for order, collection in enumerate(collections):
            CollectionProduct.objects.create(
                collection=collection, product=product, sort_order=index * 3 + order
            )

    reached = set(
        walk_every_page(
            staff_reader,
            tree,
            channel_USD,
            3,
            sortBy={"field": "COLLECTION", "direction": "ASC"},
        )
    )

    assert reached == {product.slug for product in ordered}, sorted(
        {product.slug for product in ordered} - reached
    )


# ---------------------------------------------------------------------------
# A sixth cold adversarial review (round 6, at 9b46fbe) found that the
# `COLLECTION` sentinel aggregate ran a psc-naming statement OUTSIDE the
# degradation guard, so a genuine table loss under that one sort was a hard 500
# repeating for the whole TTL; and that the round-5 relation-name requirement was
# a bare substring test that real Postgres errors walk straight through. Its
# probes are folded in below.
# ---------------------------------------------------------------------------


def test_a_collection_sort_degrades_on_a_genuine_table_loss(
    staff_reader, big_two_leg_tree, channel_USD, real_probe_ttl
):
    # given the one sort whose statements the guard did not cover. `aggregate()`
    # runs one statement per leg and the secondary leg's names the table, so a
    # true table loss mid-interval raised ProgrammingError out to the resolver
    # instead of degrading, and nothing flipped the probe, so it repeated for the
    # rest of the interval. The rest of the degradation suite runs the default
    # sort or RANK, which is why 177 tests were green with this open.
    tree, _ordered = big_two_leg_tree
    assert (
        query_category_products(staff_reader, tree, channel_USD, first=5)["totalCount"]
        == LEG_SIZE * 2
    )

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE product_secondary_categories")

    # when a COLLECTION-sorted page is requested inside the cached-True TTL
    body = raw_category_products(
        staff_reader,
        tree,
        channel=channel_USD.slug,
        first=5,
        sortBy={"field": "COLLECTION", "direction": "ASC"},
    )

    # then it degrades to what upstream would have returned, at 200
    assert "errors" not in body, body["errors"][0]["message"]
    assert body["data"]["category"]["products"]["totalCount"] == LEG_SIZE


# Real wire text, captured from psycopg 3.2.9 against Postgres 16 rather than
# constructed by hand: the whole class of defect here is a synthetic error
# passing a layer a real one evades, and this file has now had to fix that twice.
# 42501 does NOT schema-qualify and does NOT quote; 42P01 quotes, and puts the
# schema INSIDE the quotes when the statement qualified it. `diag.table_name` is
# empty for both on this server, which is why the name is parsed out of
# `message_primary` rather than read from a structured field.
REAL_42P01 = 'relation "{name}" does not exist'
REAL_42501 = "permission denied for table {name}"


@pytest.mark.parametrize(
    ("message", "sqlstate"),
    [
        # PartsLogic's migrator "creates, drops, renames and rebuilds this table
        # while Saleor is running", which is exactly the operation that produces
        # these names. A substring test reads every one as a verdict on ours.
        (REAL_42P01.format(name=f"{SECONDARY_CATEGORIES_TABLE}_v2"), "42P01"),
        (REAL_42P01.format(name=f"{SECONDARY_CATEGORIES_TABLE}_old"), "42P01"),
        (REAL_42P01.format(name=f"wsm_{SECONDARY_CATEGORIES_TABLE}"), "42P01"),
        (REAL_42501.format(name=f"{SECONDARY_CATEGORIES_TABLE}_backup"), "42501"),
        # ... and the LINE echo: Postgres appends a window of the failing
        # statement to catalog errors, and the failing statement is by
        # construction one that names our table, so the echo of our own SQL
        # carries our name into a message about something else entirely.
        (
            REAL_42P01.format(name="nope_tbl")
            + '\nLINE 1: SELECT 1 FROM "product_secondary_categories", "nope_tbl"'
            + "\n                                                       ^",
            "42P01",
        ),
    ],
)
def test_a_relation_that_is_merely_near_ours_is_not_a_verdict_on_ours(
    message, sqlstate
):
    assert (
        wsm_models.is_table_unavailable(
            driver_error(ProgrammingError(message), sqlstate)
        )
        is False
    ), f"{message!r} was accepted as a verdict on {SECONDARY_CATEGORIES_TABLE}"


@pytest.mark.parametrize(
    ("message", "sqlstate"),
    [
        (REAL_42P01.format(name=SECONDARY_CATEGORIES_TABLE), "42P01"),
        # the schema goes inside the quotes when the statement qualified it
        (REAL_42P01.format(name=f"public.{SECONDARY_CATEGORIES_TABLE}"), "42P01"),
        (REAL_42501.format(name=SECONDARY_CATEGORIES_TABLE), "42501"),
        # a column-type change on the psc table: the union leg compiles
        # `bigint = ANY (ARRAY(... text ...))` and Postgres raises 42883 at plan
        # time. It carries no relation name, so it is recognised on the SQLSTATE
        # alone and leans on the corroboration layer.
        ("operator does not exist: bigint = text", "42883"),
    ],
)
def test_the_real_verdicts_on_our_table_are_still_recognised(message, sqlstate):
    # the other half: tightening the check must not stop it recognising the cases
    # it exists for, in the exact text the server actually sends.
    assert (
        wsm_models.is_table_unavailable(
            driver_error(ProgrammingError(message), sqlstate)
        )
        is True
    ), message


def test_a_real_column_type_change_is_recognised_as_a_verdict(db):
    # Fold of the R7 repro, against the live driver rather than a hand-built error.
    # PartsLogic's rebuild can ship the psc table with a column retyped (product_id
    # as text). The secondary leg then compiles `bigint = ANY (ARRAY(... text ...))`
    # and Postgres raises 42883 at plan time, an error that carries NO relation
    # name. Before it was allowlisted it escaped is_table_unavailable and 500'd
    # every category page indefinitely, because the probe never checks column types
    # so the feature never degraded and never self-healed.
    caught = None
    try:
        # a real operator-does-not-exist (integer = text), isolated in a savepoint
        # so the aborted statement does not poison the surrounding test transaction
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 WHERE 1 = ANY (ARRAY(SELECT 'x'::text))")
    except ProgrammingError as error:
        caught = error

    assert caught is not None, "the bad cast did not raise"
    assert caught.__cause__.sqlstate == "42883", caught.__cause__.sqlstate
    assert wsm_models.is_table_unavailable(caught) is True


def test_a_union_operand_mismatch_is_not_a_table_verdict():
    # 42804 is what Postgres raises for `UNION types X and Y cannot be matched`.
    # The guarded page statement is the fork's only UNION and its corroborator
    # carries none, so accepting 42804 would let a broken union degrade to a
    # wrong page at HTTP 200 with the probe flipped off. Measured: no psc column
    # rebuild across 19 types ever raises 42804 (they raise 42883), so nothing is
    # lost by refusing it.
    error = driver_error(
        ProgrammingError("UNION types bigint and text cannot be matched"), "42804"
    )
    assert wsm_models.is_table_unavailable(error) is False


def test_secondary_categories_enabled_by_default():
    # env unset in the test environment, so the box-level gate is open and the
    # probe alone decides
    assert wsm_models.SECONDARY_CATEGORIES_ENABLED is True


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        ("", True),
        ("on", True),
        ("true", True),
        ("1", True),
        ("off", False),
        ("false", False),
        ("0", False),
        ("OFF", False),
        ("  false  ", False),
        # quote-wrapped values from a sloppy env file still read as intended
        ('"false"', False),
        ("'on'", True),
        # unrecognised spellings are an operator trying to turn it OFF: fail closed
        ("no", False),
        ("disable", False),
        ("nope", False),
    ],
)
def test_the_env_gate_disables_only_on_an_explicit_off(monkeypatch, value, enabled):
    # given a value for the per-box kill switch
    monkeypatch.setenv("WSM_SECONDARY_CATEGORIES", value)

    # then only a known on-spelling keeps the gate open; off-spellings AND
    # anything unrecognised close it
    assert wsm_models.secondary_categories_env_enabled() is enabled


def test_an_unrecognised_env_gate_value_is_logged(monkeypatch, caplog):
    # given a value nobody documented
    monkeypatch.setenv("WSM_SECONDARY_CATEGORIES", "nope")

    # when the gate is read
    with caplog.at_level("WARNING", logger="saleor.wsm.models"):
        assert wsm_models.secondary_categories_env_enabled() is False

    # then the operator can find out why the feature is off
    assert any("not a recognised value" in r.message for r in caplog.records)


def test_the_env_gate_off_answers_unavailable_without_probing(monkeypatch):
    # given the box-level gate closed
    monkeypatch.setattr(wsm_models, "SECONDARY_CATEGORIES_ENABLED", False)
    monkeypatch.setattr(
        wsm_models,
        "_probe_secondary_categories",
        lambda name: pytest.fail("the probe ran while the env gate was off"),
    )
    clear_secondary_categories_cache()

    # then the feature reports unavailable and the table is never touched
    assert secondary_categories_available("default") is False


def test_the_env_gate_on_consults_the_probe(monkeypatch):
    # given the gate open, which is its default
    monkeypatch.setattr(wsm_models, "SECONDARY_CATEGORIES_ENABLED", True)
    probed = []
    monkeypatch.setattr(
        wsm_models,
        "_probe_secondary_categories",
        lambda name: probed.append(name) or True,
    )
    clear_secondary_categories_cache()

    # then availability is decided by the probe
    assert secondary_categories_available("default") is True
    assert probed == ["default"]


def test_the_fixture_prices_disagree_with_the_tiebreak_order(big_two_leg_tree):
    # given the binding-scale fixture. `ProductOrderField.PRICE` is
    # ["min_variants_price_amount", "name", "slug"], so if price ascends alongside
    # slug then a completely broken price annotation still produces the right page
    # through the tiebreak alone and every PRICE assertion is vacuous. A cold
    # review proved that by neutering the annotation with everything still green.
    # This pins the fixture property the price assertions rest on, so a future
    # edit to the pricing cannot quietly hollow them out again.
    _tree, ordered = big_two_leg_tree
    by_slug = [product.slug for product in ordered]
    by_price = [
        product.slug
        for product in sorted(
            ordered,
            key=lambda product: ProductVariantChannelListing.objects.get(
                variant__product=product
            ).price_amount,
        )
    ]
    assert by_price == list(reversed(by_slug)), by_price
