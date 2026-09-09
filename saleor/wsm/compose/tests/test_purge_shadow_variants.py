# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What the purge is allowed to delete, and what stops it.

The fixture is the shape measured on the bake-off copy, plus the one row that
database does not have: a merchant variant whose SKU looks exactly like a shadow
(`<stock number>:900-0-9`) but which carries no marker. It is here because the
SKU shape is not the marker and a purge that keyed on it would eat real rows.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from prices import Money, TaxedMoney

from saleor.channel.models import Channel
from saleor.checkout.models import CheckoutLine
from saleor.order.models import OrderLine
from saleor.product import ProductTypeKind
from saleor.product.models import (
    Product,
    ProductChannelListing,
    ProductType,
    ProductVariant,
)
from saleor.wsm.compose.management.commands import purge_shadow_option_variants as purge

CHANNEL = "fuelab"
SKUS = ("FMBG-40401", "FMBG-62810-0")


@pytest.fixture
def catalog(db):
    """Two products, each with a base variant, two shadows and one lookalike."""
    channel = Channel.objects.create(
        name="Fuel Lab", slug=CHANNEL, currency_code="USD", default_country="US"
    )
    product_type = ProductType.objects.create(
        name="Part", slug="part", kind=ProductTypeKind.NORMAL
    )
    made = {}
    for sku in SKUS:
        product = Product.objects.create(
            name=sku, slug=sku.lower(), product_type=product_type
        )
        ProductChannelListing.objects.create(
            product=product, channel=channel, is_published=True, currency="USD"
        )
        made[sku] = {
            "product": product,
            "base": ProductVariant.objects.create(
                product=product, sku=sku, name="Base"
            ),
            "shadows": [
                ProductVariant.objects.create(
                    product=product,
                    sku=f"{sku}:900-0-{n}",
                    name=f"Shadow {n}",
                    metadata={purge.MARKER: "900"},
                )
                for n in (1, 2)
            ],
            "twin": ProductVariant.objects.create(
                product=product, sku=f"{sku}:900-0-9", name="Merchant colourway"
            ),
        }
    return made


def _order_line(order, variant):
    price = TaxedMoney(net=Money("34.00", "USD"), gross=Money("34.00", "USD"))
    return OrderLine.objects.create(
        order=order,
        variant=variant,
        product_name=variant.product.name,
        variant_name=variant.name,
        product_sku=variant.sku,
        product_variant_id=variant.get_global_id(),
        is_shipping_required=False,
        is_gift_card=False,
        quantity=1,
        unit_price=price,
        total_price=price,
    )


def test_the_marker_selects_the_candidates_and_the_lookalike_survives(catalog):
    report = purge.purge(CHANNEL)

    assert report["candidates"] == 4
    assert report["deleted"] == 4
    assert set(ProductVariant.objects.values_list("sku", flat=True)) == {
        "FMBG-40401",
        "FMBG-40401:900-0-9",
        "FMBG-62810-0",
        "FMBG-62810-0:900-0-9",
    }


def test_an_order_line_protects_a_shadow_variant(catalog, order):
    kept = catalog[SKUS[0]]["shadows"][1]
    _order_line(order, kept)

    report = purge.purge(CHANNEL)

    assert report["referenced_by_order"] == 1
    assert report["referenced_by_order_skus"] == [kept.sku]
    assert report["deleted"] == 3
    assert ProductVariant.objects.filter(pk=kept.pk).exists()


def test_a_variant_a_checkout_still_holds_is_ambiguous_not_deleted(catalog, checkout):
    held = catalog[SKUS[0]]["shadows"][0]
    CheckoutLine.objects.create(
        checkout=checkout, variant=held, quantity=1, currency="USD"
    )

    report = purge.purge(CHANNEL)

    assert report["ambiguous"] == 1
    assert report["ambiguous_skus"] == [held.sku]
    assert report["ambiguous_because"] == {"in a checkout": 1}
    assert report["deleted"] == 3
    assert ProductVariant.objects.filter(pk=held.pk).exists()


def test_a_candidate_without_the_marker_refuses_the_whole_run(catalog, monkeypatch):
    """The marker is re-read inside the transaction, so a widened survey refuses."""
    innocent = catalog[SKUS[0]]["base"]
    surveyed = purge.survey

    def widened(channel_slug):
        report = surveyed(channel_slug)
        report["deletable_ids"].add(innocent.pk)
        return report

    monkeypatch.setattr(purge, "survey", widened)

    with pytest.raises(CommandError, match="do not carry"):
        purge.purge(CHANNEL)

    assert ProductVariant.objects.count() == 8


def test_a_changed_survivor_count_refuses_and_rolls_back(catalog, monkeypatch):
    """Doctor the AFTER census so a product looks like it gained a variant."""
    census = purge._surviving_census

    def doctored(doomed_ids):
        counts = census(doomed_ids)
        if not doomed_ids:  # the after-delete census, taken over everything left
            counts[min(counts)] += 1
        return counts

    monkeypatch.setattr(purge, "_surviving_census", doctored)

    with pytest.raises(CommandError, match="rolled back"):
        purge.purge(CHANNEL)

    assert ProductVariant.objects.count() == 8
    assert ProductVariant.objects.filter(metadata__has_key=purge.MARKER).count() == 4


def test_a_second_run_deletes_nothing(catalog):
    assert purge.purge(CHANNEL)["deleted"] == 4

    again = purge.purge(CHANNEL)

    assert again["candidates"] == 0
    assert again["deleted"] == 0


def test_the_command_counts_until_apply_is_asked_for(catalog, capsys):
    call_command("purge_shadow_option_variants", "--site", CHANNEL)

    assert ProductVariant.objects.filter(metadata__has_key=purge.MARKER).count() == 4
    assert "deletable: 4" in capsys.readouterr().out

    call_command("purge_shadow_option_variants", "--site", CHANNEL, "--apply")

    assert not ProductVariant.objects.filter(metadata__has_key=purge.MARKER).exists()
