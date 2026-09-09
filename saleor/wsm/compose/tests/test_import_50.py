# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The 5.0 mapping, on a payload shaped exactly like Fuel Lab's rows.

The MySQL read is a separate function on purpose, so everything below runs the
real `apply()` against real rows in our own tables with no server anywhere. The
payload is a trimmed copy of what Fuel Lab actually holds: a Color set whose
dealer prices are DUPLICATE value rows carrying the group name in `desc`, and
the QSST axes whose selections have to add to 2943.00.
"""

import json
from decimal import Decimal

import pytest

from saleor.channel.models import Channel
from saleor.product import ProductTypeKind
from saleor.product.models import (
    Product,
    ProductChannelListing,
    ProductType,
    ProductVariant,
    ProductVariantChannelListing,
)
from saleor.wsm.compose import pricing
from saleor.wsm.compose.management.commands.import_option_sets_50 import (
    CONFIGURABLE_METAFIELD,
    apply,
)
from saleor.wsm.compose.models import (
    PRICE_FLOOR_METAFIELD,
    DealerTierOptionPrice,
    OptionSet,
    OptionValue,
)
from saleor.wsm.compose.pricing import Selection, price_configured
from saleor.wsm.dealer.models import DealerGroup, TierPrice

CHANNEL = "fuelab"
QSST_SKU = "FMBG-62810-0"
COLOR_SKU = "FMBG-40401"

GROUPS = [
    {"id": 4005, "name": "Dealer 1", "is_price_group": 1, "active": 1},
    {"id": 5619, "name": "Dealer 2", "is_price_group": 1, "active": 1},
    {"id": 5621, "name": "Test Dealer", "is_price_group": 0, "active": 1},
]


def _set(set_id, sku, name, priority, type_="enum", hidden=0, required=1):
    return {
        "set_id": set_id,
        "sku": sku,
        "name": name,
        "label": name + "?",
        "type": type_,
        "required": required,
        "hidden": hidden,
        "priority": priority,
        "deselect": "",
        "description": f"<strong>{name}</strong>",
    }


def _value(set_id, value_id, name, desc, price, sku, priority, image=0, ext=None):
    return {
        "set_id": set_id,
        "value_id": value_id,
        "name": name,
        "desc": desc,
        "price": price,
        "sku": sku,
        "priority": priority,
        "image": image,
        "image_ext": ext,
    }


@pytest.fixture
def payload():
    """Fuel Lab's two shapes: tiered Color values, and the five QSST axes."""
    sets = [
        _set(1, COLOR_SKU, "Color", 900),
        _set(103, QSST_SKU, "QSST Add Lift Pump", 500),
        _set(104, QSST_SKU, "QSST Add Surge Tank Pump", 490),
        _set(105, QSST_SKU, "QSST Add Fuel Filter Neck", 480),
        _set(106, QSST_SKU, "QSST Add Fuel Level Sensor", 470),
        _set(107, QSST_SKU, "QSST Add A Fuel Cell Vent Kit", 460),
    ]
    values = [
        _value(1, 1, "Black", "Retail", "0.00", "1", 1, 198651229, "jpg"),
        _value(1, 4, "Black", "Dealer 1", "0.00", "1", 2, 198651227, "jpg"),
        _value(1, 7, "Black", "Dealer 2", "0.00", "1", 3, 198651228, "jpg"),
        _value(1, 2, "Red", "Retail", "34.00", "2", 4, 198651235, "jpg"),
        _value(1, 5, "Red", "Dealer 1", "32.30", "2", 5, 198651233, "jpg"),
        _value(1, 8, "Red", "Dealer 2", "21.97", "2", 6, 198651234, "jpg"),
        _value(103, 1540, "No lift pump", "Retail", "0.00", "", 1),
        _value(103, 1541, "FUELAB 340LPH Lift Pump", "Retail", "125.00", "494xx", 2),
        _value(
            103,
            1543,
            "FUELAB 500LPH Brushless Pump w/ Controller",
            "Retail",
            "500.00",
            "49614",
            3,
        ),
        _value(104, 1544, "No Surge Pump", "Retail", "0.00", "", 1),
        _value(
            104,
            1767,
            "Twin Screw Brushless Pump 20815 1100LPH",
            "Retail",
            "1550.00",
            "20815",
            8,
        ),
        _value(105, 1550, "No - Open hole", "Retail", "0.00", "", 1),
        _value(106, 1554, "No - Cover for Hole Included", "Retail", "0.00", "", 1),
        _value(107, 1556, "No - I will make my own vent kit", "Retail", "0.00", "", 1),
    ]
    return {
        "sets": sets,
        "values": values,
        "groups": GROUPS,
        "fees": [],
        "tier_prices": [
            {"sku": COLOR_SKU, "group": "Dealer 1", "price": "180.500", "quantity": 1},
            {"sku": COLOR_SKU, "group": "Dealer 2", "price": "160.000", "quantity": 5},
        ],
    }


@pytest.fixture
def catalog(db):
    """Two products in their own channel, each with a base variant named by its SKU."""
    channel = Channel.objects.create(
        name="Fuel Lab", slug=CHANNEL, currency_code="USD", default_country="US"
    )
    product_type = ProductType.objects.create(
        name="Part", slug="part", kind=ProductTypeKind.NORMAL
    )
    products = {}
    for sku, name in ((QSST_SKU, "QSST 2.0 Titanium"), (COLOR_SKU, "85GPH Pump")):
        product = Product.objects.create(
            name=name, slug=sku.lower(), product_type=product_type
        )
        ProductChannelListing.objects.create(
            product=product, channel=channel, is_published=True, currency="USD"
        )
        # The older import's shadow variant, which must never be taken for the
        # product's own dealer price. Created FIRST so an importer that took
        # "the product's first variant" would take this one.
        ProductVariant.objects.create(
            product=product, sku=f"{sku}:900-0-1", name="shadow"
        )
        ProductVariant.objects.create(product=product, sku=sku, name=name)
        products[sku] = product
    return products


def test_counts_and_shapes(payload, catalog):
    report = apply(
        payload, channel_slug=CHANNEL, image_base="https://fuelab.com/images"
    )

    assert report["sets_created"] == 6
    # Retail rows only: the Dealer 1 and Dealer 2 duplicates are not values.
    assert report["values_created"] == 10
    assert report["tiers_created"] == 4
    assert report["groups_created"] == 2  # Test Dealer is not a price group
    assert report.get("sets_unmatched", 0) == 0

    assert OptionValue.objects.filter(name="Black").count() == 1
    assert set(DealerGroup.objects.values_list("code", flat=True)) == {
        "dealer-1",
        "dealer-2",
    }
    red = OptionValue.objects.get(name="Red")
    assert red.price_delta == Decimal("34.00")
    assert red.image_url == "https://fuelab.com/images/F198651235.jpg"
    assert dict(red.tier_deltas.values_list("tier_group", "price_delta")) == {
        "dealer-1": Decimal("32.30"),
        "dealer-2": Decimal("21.97"),
    }

    lift = OptionSet.objects.get(name="QSST Add Lift Pump")
    assert lift.prompt_type == pricing.CHOICE_ONE
    assert lift.required is True
    assert lift.sort_order == 500

    for product in catalog.values():
        product.refresh_from_db()
        assert product.metadata[CONFIGURABLE_METAFIELD] == "true"


def test_the_importer_stamps_the_price_floor(payload, catalog):
    """An imported catalog leaves the same stamp a hand-built one does.

    The `catalog` fixture prices nothing, so the listing is added here: a floor
    needs a base price, and a product with none is stamped with nothing rather
    than with a zero. The charge is required, so it is part of the lowest price
    anyone pays and the floor is 100.00 + 149.00, not the base.
    """
    channel = Channel.objects.get(slug=CHANNEL)
    product = catalog[COLOR_SKU]
    ProductVariantChannelListing.objects.create(
        variant=product.variants.get(sku=COLOR_SKU),
        channel=channel,
        currency="USD",
        price_amount=Decimal("100.00"),
        discounted_price_amount=Decimal("100.00"),
    )
    payload["fees"] = [
        {
            "sku": COLOR_SKU,
            "fee_label": "Crating",
            "fee_sku": "CRATE",
            "fee": "149.00",
            "return_first": 0,
            "return_first_label": "",
        }
    ]

    report = apply(payload, channel_slug=CHANNEL)

    assert report["fees_created"] == 1
    product.refresh_from_db()
    assert json.loads(product.metadata[PRICE_FLOOR_METAFIELD]) == {
        CHANNEL: {"amount": "249.00", "currency": "USD"}
    }
    # The other product is priced in no channel, so it carries the marker and
    # no floor: a half-built catalog gets no number rather than a wrong one.
    other = catalog[QSST_SKU]
    other.refresh_from_db()
    assert other.metadata[CONFIGURABLE_METAFIELD] == "true"
    assert PRICE_FLOOR_METAFIELD not in other.metadata

    # A base price moved with no Compose row touched: no save signal fires, so
    # the import's own stamping pass is the only thing that makes the stamp
    # current. That is the case the pass exists for, and the counter proves it
    # ran rather than the signals having got there first.
    ProductVariantChannelListing.objects.filter(variant__product=product).update(
        price_amount=Decimal("300.00")
    )
    again = apply(payload, channel_slug=CHANNEL)

    assert again["price_floor_written"] == 1
    assert again.get("fees_created", 0) == 0
    product.refresh_from_db()
    assert json.loads(product.metadata[PRICE_FLOOR_METAFIELD]) == {
        CHANNEL: {"amount": "449.00", "currency": "USD"}
    }


def test_tier_price_lands_on_the_base_variant_not_the_shadow(payload, catalog):
    apply(payload, channel_slug=CHANNEL)

    rows = TierPrice.objects.select_related("variant", "group")
    assert rows.count() == 2
    assert {r.variant.sku for r in rows} == {COLOR_SKU}
    by_group = {r.group.code: (r.min_quantity, r.amount) for r in rows}
    assert by_group["dealer-1"] == (1, Decimal("180.500"))
    assert by_group["dealer-2"] == (5, Decimal("160.000"))


def test_second_run_changes_nothing(payload, catalog):
    apply(payload, channel_slug=CHANNEL, image_base="https://fuelab.com/images")
    again = apply(payload, channel_slug=CHANNEL, image_base="https://fuelab.com/images")

    for key in ("sets", "values", "tiers", "groups", "tier_prices"):
        assert again.get(f"{key}_created", 0) == 0, key
        assert again.get(f"{key}_updated", 0) == 0, key
    assert again["sets_unchanged"] == 6
    assert again["values_unchanged"] == 10
    assert again["tiers_unchanged"] == 4
    assert again.get("metafield_written", 0) == 0
    assert again["values_stale"] == 0


def test_hidden_sets_are_refused_unless_asked_for(payload, catalog):
    payload["sets"].append(_set(200, QSST_SKU, "Internal note", 100, hidden=1))

    refused = apply(payload, channel_slug=CHANNEL)
    assert refused["sets_hidden_refused"] == 1
    assert not OptionSet.objects.filter(name="Internal note").exists()

    included = apply(payload, channel_slug=CHANNEL, include_hidden=True)
    assert included["sets_created"] == 1
    assert OptionSet.objects.filter(name="Internal note").exists()


def test_unmatched_sku_is_reported_not_invented(payload, catalog):
    payload["sets"].append(_set(300, "NOT-IN-SALEOR", "Ghost", 10))

    report = apply(payload, channel_slug=CHANNEL)
    assert report["sets_unmatched"] == 1
    assert report["unmatched_skus"] == ["NOT-IN-SALEOR"]
    assert not OptionSet.objects.filter(name="Ghost").exists()


def test_a_desc_that_names_no_group_is_not_a_tier(payload, catalog):
    payload["values"].append(_value(1, 99, "Red", "Clearance", "5.00", "2", 7))

    report = apply(payload, channel_slug=CHANNEL)
    assert report["values_desc_not_a_group"] == 1
    assert report["unknown_desc"] == ["Color: Clearance"]
    assert DealerTierOptionPrice.objects.filter(tier_group="clearance").count() == 0


def test_imported_qsst_rows_price_the_real_configuration(payload, catalog):
    """893 base + 1550 surge pump + 500 lift pump = 2943.00, from imported rows."""
    apply(payload, channel_slug=CHANNEL)

    product = catalog[QSST_SKU]
    sets = list(OptionSet.objects.filter(product=product).prefetch_related("values"))
    assert len(sets) == 5

    chosen = {
        "QSST Add Lift Pump": "FUELAB 500LPH Brushless Pump w/ Controller",
        "QSST Add Surge Tank Pump": "Twin Screw Brushless Pump 20815 1100LPH",
        "QSST Add Fuel Filter Neck": "No - Open hole",
        "QSST Add Fuel Level Sensor": "No - Cover for Hole Included",
        "QSST Add A Fuel Cell Vent Kit": "No - I will make my own vent kit",
    }
    selections = [
        Selection(
            set_id=s.pk,
            value_ids=(s.values.get(name=chosen[s.name]).pk,),
        )
        for s in sets
    ]
    priced = price_configured(89300, [s.to_pricing() for s in sets], selections, None)
    assert priced.unit_cents == 294300


def test_a_dealer_pays_the_tier_delta_on_an_imported_value(payload, catalog):
    apply(payload, channel_slug=CHANNEL)

    color = OptionSet.objects.get(name="Color")
    red = color.values.get(name="Red")
    selections = [Selection(set_id=color.pk, value_ids=(red.pk,))]

    retail = price_configured(10000, [color.to_pricing()], selections, None)
    dealer = price_configured(10000, [color.to_pricing()], selections, "dealer-2")
    assert retail.unit_cents == 13400
    assert dealer.unit_cents == 12197


# --- verdict "the 5.0 importer never full_clean()s" -------------------------
#
# Every rule this import can break is a model `clean()`, and none of them ran:
# the import wrote the row, said it worked, and the MERCHANT met the refusal
# later, from a screen that would not save until they fixed a row they had not
# written. A refused row is a reported skip now: the run finishes, everything
# sound lands, and the report names what did not and why.

LONG_GROUP = "Dealer Tier 1 " + "West Coast Distributor " * 5


@pytest.fixture
def priced_catalog(catalog):
    """A listed price, which is what turns the configured floor into a number."""
    channel = Channel.objects.get(slug=CHANNEL)
    variant = ProductVariant.objects.get(sku=COLOR_SKU)
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel,
        price_amount=Decimal("10.00"),
        discounted_price_amount=Decimal("10.00"),
        currency="USD",
    )
    return catalog


def test_a_value_that_prices_the_product_below_zero_is_a_reported_skip(
    payload, priced_catalog
):
    """A 500.00 credit on a 10.00 product: the configurator would price at -490."""
    payload["values"].append(_value(1, 97, "Bargain", "Retail", "-500.00", "3", 9))

    report = apply(payload, channel_slug=CHANNEL)

    assert report["values_refused"] == 1
    assert not OptionValue.objects.filter(name="Bargain").exists()
    assert [entry for entry in report["refused"] if "Bargain" in entry]
    # The run still finished and the rest of the tenant's configurator landed.
    assert report["values_created"] == 10
    assert report["sets_created"] == 6


def test_a_tier_row_naming_no_dealer_group_is_a_reported_skip(payload, catalog):
    """5.0 group names are free text and our group code is 100 characters.

    The group is refused, so the code its tier rows name exists nowhere, and a
    tier row against a group nobody belongs to is a price that can never be
    charged: written before, reported now, with the retail row it hangs off
    still imported.
    """
    payload["groups"].append(
        {"id": 6000, "name": LONG_GROUP, "is_price_group": 1, "active": 1}
    )
    payload["values"].append(_value(1, 98, "Red", LONG_GROUP, "30.00", "2", 9))

    report = apply(payload, channel_slug=CHANNEL)

    assert report["groups_refused"] == 1
    assert report["tiers_refused"] == 1
    assert report["groups_created"] == 2
    assert DealerGroup.objects.count() == 2
    assert DealerTierOptionPrice.objects.count() == 4
    assert any("no dealer group" in entry for entry in report["refused"])
    # The choice itself is sound, so it is imported: one bad dealer row does not
    # cost the merchant the answer their shoppers pick.
    assert OptionValue.objects.filter(name="Red").count() == 1
