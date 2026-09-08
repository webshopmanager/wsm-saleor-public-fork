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
    }


def test_stamping_leaves_other_metadata_alone(collection, product_list):
    collection.store_value_in_metadata({"someone.elses": "key"})
    collection.save(update_fields=["metadata"])

    series_for(collection).save()

    collection.refresh_from_db()
    assert collection.metadata["someone.elses"] == "key"
    assert SERIES_METADATA_KEY in collection.metadata


def test_the_merchant_screens_register(db):
    """The admin lives on the AdminSite U2 mounts, absent here: probe one."""
    from django.contrib.admin import AdminSite

    from saleor.wsm.containers import admin as containers_admin

    site = containers_admin.register(AdminSite(name="probe"))

    assert set(site._registry) == {SeriesConfig, KitConfig}
    assert site._registry[SeriesConfig].raw_id_fields == ("collection",)
    kit_admin = site._registry[KitConfig]
    assert kit_admin.raw_id_fields == ("collection",)
    assert kit_admin.inlines[0].model is KitMember
    assert kit_admin.inlines[0].raw_id_fields == ("variant",)
