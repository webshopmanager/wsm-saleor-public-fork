# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The series editor over GraphQL, through the real view.

Every assertion goes through `post_graphql`, which under this branch hits the
composed schema mounted by `saleor/wsm/urls.py`. A resolver called by hand
cannot tell a field that exists from a field that is SERVED.

The delete case is the one that matters most: `wsm.series` on the Collection is
DERIVED OUTPUT of this row (models.SeriesConfig docstring, Dana 2026-09-09), so
a delete that leaves the blob behind leaves a series live on the storefront and
in the search index with no row anywhere to edit it.
"""

import json

import graphene
import pytest

from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from ....containers.models import SERIES_METADATA_KEY, SeriesConfig

pytestmark = pytest.mark.django_db


UPDATE = """
    mutation Update($collection: ID!, $input: WsmSeriesConfigInput!) {
      wsmSeriesConfigUpdate(collection: $collection, input: $input) {
        series {
          id
          brand
          axes
          partitioningAxis
          missMessage
          published
          memberCount
          collection { id name }
        }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation Delete($id: ID!) {
      wsmSeriesConfigDelete(id: $id) {
        series { id brand }
        errors { field code message }
      }
    }
"""

DETAIL = """
    query Detail($id: ID, $collection: ID) {
      wsmSeriesConfig(id: $id, collection: $collection) {
        id
        brand
        axes
        partitioningAxis
        published
        memberCount
      }
    }
"""

LIST = """
    query List($filter: WsmSeriesConfigFilterInput) {
      wsmSeriesConfigs(filter: $filter, first: 20) {
        totalCount
        edges { node { id brand published } }
      }
    }
"""


def collection_id(collection):
    return graphene.Node.to_global_id("Collection", collection.pk)


def series_gid(series):
    return graphene.Node.to_global_id("WsmSeriesConfig", series.pk)


def draft(**overrides):
    payload = {
        "brand": "WeatherTech",
        "axes": ["color"],
        "partitioningAxis": "color",
        "missMessage": "Nothing in this series fits that truck yet.",
        "published": False,
    }
    payload.update(overrides)
    return payload


def run_update(client, collection, permission, **overrides):
    return client.post_graphql(
        UPDATE,
        {"collection": collection_id(collection), "input": draft(**overrides)},
        permissions=[permission],
    )


# --- permissions -------------------------------------------------------------


def test_an_anonymous_caller_cannot_read_a_series(api_client, collection):
    response = api_client.post_graphql(
        DETAIL, {"collection": collection_id(collection)}
    )

    assert_no_permission(response)


def test_staff_without_manage_products_cannot_read_a_series(
    staff_api_client, collection
):
    response = staff_api_client.post_graphql(
        DETAIL, {"collection": collection_id(collection)}
    )

    assert_no_permission(response)


def test_the_update_is_refused_without_the_permission(staff_api_client, collection):
    response = staff_api_client.post_graphql(
        UPDATE, {"collection": collection_id(collection), "input": draft()}
    )

    assert_no_permission(response)
    assert not SeriesConfig.objects.exists()


def test_the_delete_is_refused_without_the_permission(
    staff_api_client, collection, product_list
):
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )

    response = staff_api_client.post_graphql(DELETE, {"id": series_gid(series)})

    assert_no_permission(response)
    assert SeriesConfig.objects.filter(pk=series.pk).exists()


# --- the upsert --------------------------------------------------------------


def test_the_update_creates_the_row_and_stamps_the_collection(
    staff_api_client, permission_manage_products, collection, product_list
):
    """One call creates the row. The blob is written in the same breath."""
    collection.products.add(*product_list[:2])

    response = run_update(
        staff_api_client, collection, permission_manage_products, published=True
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert payload["errors"] == []
    assert payload["series"]["brand"] == "WeatherTech"
    assert payload["series"]["axes"] == ["color"]
    assert payload["series"]["partitioningAxis"] == "color"
    assert payload["series"]["published"] is True
    assert payload["series"]["memberCount"] == 2
    assert payload["series"]["collection"]["name"] == collection.name

    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY]) == {
        "brand": "WeatherTech",
        "axes": ["color"],
        "partitioning_axis": "color",
        "miss_message": "Nothing in this series fits that truck yet.",
        "published": True,
    }


def test_the_update_edits_the_row_it_already_made_and_never_a_second_one(
    staff_api_client, permission_manage_products, collection, product_list
):
    """A OneToOne keyed by its collection: there is no second row to make."""
    collection.products.add(*product_list[:2])
    staff_api_client.user.user_permissions.add(permission_manage_products)

    for brand in ("WeatherTech", "Husky"):
        response = staff_api_client.post_graphql(
            UPDATE,
            {"collection": collection_id(collection), "input": draft(brand=brand)},
        )
        assert (
            get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]["errors"]
            == []
        )

    assert SeriesConfig.objects.count() == 1
    assert SeriesConfig.objects.get().brand == "Husky"
    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY])["brand"] == "Husky"


def test_an_omitted_field_leaves_the_stored_value_alone(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.products.add(*product_list[:2])
    staff_api_client.user.user_permissions.add(permission_manage_products)
    staff_api_client.post_graphql(
        UPDATE, {"collection": collection_id(collection), "input": draft()}
    )

    response = staff_api_client.post_graphql(
        UPDATE, {"collection": collection_id(collection), "input": {"brand": "Husky"}}
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert payload["errors"] == []
    assert payload["series"]["axes"] == ["color"]
    assert payload["series"]["partitioningAxis"] == "color"


# --- the gate, each refusal carrying its own code ----------------------------


def test_publishing_with_a_partitioning_axis_outside_the_axes_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.products.add(*product_list[:2])

    response = run_update(
        staff_api_client,
        collection,
        permission_manage_products,
        axes=["color"],
        partitioningAxis="size",
        published=True,
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert payload["series"] is None
    assert [error["code"] for error in payload["errors"]] == ["AXIS_NOT_IN_AXES"]
    assert payload["errors"][0]["field"] == "partitioningAxis"
    assert not SeriesConfig.objects.exists()


def test_publishing_a_series_with_one_member_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.products.add(product_list[0])

    response = run_update(
        staff_api_client, collection, permission_manage_products, published=True
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == [
        "SERIES_NEEDS_TWO_MEMBERS"
    ]
    assert "2 or more published" in payload["errors"][0]["message"]


def test_publishing_with_a_member_missing_the_partitioning_attribute_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.products.add(*product_list)
    naked = product_list[1]
    naked.attributevalues.all().delete()

    response = run_update(
        staff_api_client, collection, permission_manage_products, published=True
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == [
        "MEMBER_MISSING_PARTITIONING_ATTRIBUTE"
    ]
    assert naked.slug in payload["errors"][0]["message"]


def test_an_axis_the_store_has_no_attribute_for_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    """The admin form offered the store's own attributes; this is that rule.

    Typing a slug no attribute carries builds a configurator that asks a
    question nothing can answer (`containers/admin.py:56`, `:108`).
    """
    response = run_update(
        staff_api_client,
        collection,
        permission_manage_products,
        axes=["color", "not_an_attribute"],
        partitioningAxis="color",
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == ["UNKNOWN_ATTRIBUTE_SLUG"]
    assert "not_an_attribute" in payload["errors"][0]["message"]
    assert not SeriesConfig.objects.exists()


def test_a_slug_the_row_already_holds_is_never_taken_away_from_it(
    staff_api_client, permission_manage_products, collection, product_list
):
    """The admin's `(missing)` marker, as a rule rather than a label.

    A store that deletes an attribute must not have the next save of an
    unrelated field silently drop a configurator question nobody asked to lose.
    """
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color", "cab_style"],
        partitioning_axis="color",
    )

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "collection": collection_id(collection),
            "input": {"axes": ["color", "cab_style"], "brand": "Husky"},
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigUpdate"]
    assert payload["errors"] == []
    series.refresh_from_db()
    assert series.axes == ["color", "cab_style"]


# --- the delete, and what it has to take with it -----------------------------


def test_the_delete_clears_the_series_blob_from_the_collection(
    staff_api_client, permission_manage_products, collection, product_list
):
    """The proof this unit exists for: no row, no blob, no series anywhere."""
    collection.products.add(*product_list[:2])
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
        published=True,
    )
    collection.refresh_from_db()
    assert SERIES_METADATA_KEY in collection.metadata, "the fixture never stamped"

    response = staff_api_client.post_graphql(
        DELETE, {"id": series_gid(series)}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmSeriesConfigDelete"]
    assert payload["errors"] == []
    assert payload["series"]["brand"] == "WeatherTech"
    assert not SeriesConfig.objects.filter(pk=series.pk).exists()
    collection.refresh_from_db()
    assert SERIES_METADATA_KEY not in collection.metadata


def test_the_delete_leaves_other_metadata_on_the_collection_alone(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.store_value_in_metadata({"someone.elses": "key"})
    collection.save(update_fields=["metadata"])
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )

    staff_api_client.post_graphql(
        DELETE, {"id": series_gid(series)}, permissions=[permission_manage_products]
    )

    collection.refresh_from_db()
    assert collection.metadata["someone.elses"] == "key"


# --- the read surface --------------------------------------------------------


def test_the_detail_answers_by_collection_and_by_id(
    staff_api_client, permission_manage_products, collection, product_list
):
    collection.products.add(*product_list[:2])
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )
    staff_api_client.user.user_permissions.add(permission_manage_products)

    by_collection = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"collection": collection_id(collection)})
    )["data"]["wsmSeriesConfig"]
    by_id = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": series_gid(series)})
    )["data"]["wsmSeriesConfig"]

    assert by_collection == by_id
    assert by_collection["brand"] == "WeatherTech"
    assert by_collection["memberCount"] == 2


def test_the_detail_refuses_both_keys_at_once(
    staff_api_client, permission_manage_products, collection
):
    series = SeriesConfig.objects.create(
        collection=collection, brand="WeatherTech", axes=[], partitioning_axis=""
    )

    response = staff_api_client.post_graphql(
        DETAIL,
        {"id": series_gid(series), "collection": collection_id(collection)},
        permissions=[permission_manage_products],
    )

    assert "Argument" in response.json()["errors"][0]["message"]


def test_the_list_filters_by_published_brand_and_search(
    staff_api_client,
    permission_manage_products,
    collection,
    published_collection,
    product_list,
):
    SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
        published=False,
    )
    SeriesConfig.objects.create(
        collection=published_collection,
        brand="Husky",
        axes=["color"],
        partitioning_axis="color",
        published=False,
    )
    staff_api_client.user.user_permissions.add(permission_manage_products)

    def listed(filter_):
        content = get_graphql_content(
            staff_api_client.post_graphql(LIST, {"filter": filter_})
        )
        return [
            edge["node"]["brand"]
            for edge in content["data"]["wsmSeriesConfigs"]["edges"]
        ]

    assert sorted(listed({})) == ["Husky", "WeatherTech"]
    assert listed({"brand": "Husky"}) == ["Husky"]
    assert listed({"published": True}) == []
    assert listed({"search": "Husky"}) == ["Husky"]
    assert listed({"collection": collection_id(collection)}) == ["WeatherTech"]
