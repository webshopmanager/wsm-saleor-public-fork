# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The materialization gate, and the Collection metadata the storefront reads."""

import json

import pytest
from django.core.exceptions import ValidationError

from saleor.wsm.containers.models import (
    SERIES_METADATA_KEY,
    KitConfig,
    KitMember,
    SeriesConfig,
)

pytestmark = pytest.mark.django_db


def series_for(collection, **overrides):
    fields = {
        "collection": collection,
        "brand": "WeatherTech",
        "axes": ["color"],
        "partitioning_axis": "color",
        "miss_message": "Nothing in this series fits that truck yet.",
        "published": True,
    }
    fields.update(overrides)
    return SeriesConfig(**fields)


def test_gate_refuses_one_member(collection, product_list):
    """1 member is a redirect to the SKU wearing a series costume."""
    collection.products.add(product_list[0])

    with pytest.raises(ValidationError) as refusal:
        series_for(collection).clean()

    assert "2 or more published" in str(refusal.value)


def test_gate_refuses_zero_members(collection, db):
    with pytest.raises(ValidationError):
        series_for(collection).clean()


def test_gate_refuses_a_partitioning_axis_that_is_not_an_axis(collection, product_list):
    collection.products.add(*product_list)

    with pytest.raises(ValidationError) as refusal:
        series_for(collection, partitioning_axis="cab_style").clean()

    assert "cab_style" in str(refusal.value)


def test_gate_refuses_a_member_missing_the_partitioning_attribute(
    collection, product_list
):
    collection.products.add(*product_list)
    naked = product_list[1]
    naked.attributevalues.all().delete()

    with pytest.raises(ValidationError) as refusal:
        series_for(collection).clean()

    assert naked.slug in str(refusal.value)


def test_gate_ignores_unpublished_products(collection, product_list):
    """Publication is the gate's unit of counting, not membership."""
    collection.products.add(*product_list)
    for product in product_list[1:]:
        product.channel_listings.update(is_published=False)

    with pytest.raises(ValidationError) as refusal:
        series_for(collection).clean()

    assert "2 or more published" in str(refusal.value)


def test_gate_passes_with_two_members_carrying_the_axis(collection, product_list):
    collection.products.add(*product_list[:2])

    series_for(collection).clean()  # no refusal is the assertion


def test_an_unpublished_series_is_never_gated(collection, product_list):
    """A merchant builds a series before it qualifies; that must not be an error."""
    collection.products.add(product_list[0])

    series_for(collection, published=False, partitioning_axis="anything").clean()


def test_save_stamps_the_series_facts_on_the_collection(collection, product_list):
    collection.products.add(*product_list[:2])

    series_for(collection).save()

    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY]) == {
        "brand": "WeatherTech",
        "axes": ["color"],
        "partitioning_axis": "color",
        "miss_message": "Nothing in this series fits that truck yet.",
        "published": True,
    }


def test_a_draft_stamps_published_false(collection, product_list):
    """The stamp fires on every save, so the blob must carry the publish state.

    Readers key visibility off the blob (the search engine hides a series whose
    blob does not say published) and a save happens long before a series
    qualifies, so a draft that stamped nothing would be indistinguishable from
    a live one.
    """
    collection.products.add(*product_list[:2])

    series_for(collection, published=False).save()

    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY])["published"] is False


def test_unpublishing_restamps_the_blob_false(collection, product_list):
    """The flip that takes a live series dark is the one that MUST reach readers."""
    collection.products.add(*product_list[:2])
    series = series_for(collection)
    series.save()
    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY])["published"] is True

    series.published = False
    series.save()

    collection.refresh_from_db()
    assert json.loads(collection.metadata[SERIES_METADATA_KEY])["published"] is False


def test_stamping_leaves_other_metadata_alone(collection, product_list):
    collection.store_value_in_metadata({"someone.elses": "key"})
    collection.save(update_fields=["metadata"])

    series_for(collection).save()

    collection.refresh_from_db()
    assert collection.metadata["someone.elses"] == "key"
    assert SERIES_METADATA_KEY in collection.metadata


def test_the_merchant_screens_register(db):
    """The screens are on the ONE AdminSite the compose unit mounts at /admin/.

    Asserted against the real site rather than a probe now that both units sit
    on one branch: registering onto a throwaway AdminSite would still pass if
    the import at the bottom of admin.py were deleted.
    """
    from saleor.wsm.compose.admin import site

    assert {SeriesConfig, KitConfig} <= set(site._registry)
    # Autocomplete, not raw id: the picker admins in saleor/wsm/admin_pickers.py
    # give the widget a Collection and a ProductVariant list to resolve against,
    # which is what a bare id box never had.
    assert site._registry[SeriesConfig].autocomplete_fields == ("collection",)
    kit_admin = site._registry[KitConfig]
    assert kit_admin.autocomplete_fields == ("collection",)
    assert kit_admin.inlines[0].model is KitMember
    assert kit_admin.inlines[0].autocomplete_fields == ("variant",)
