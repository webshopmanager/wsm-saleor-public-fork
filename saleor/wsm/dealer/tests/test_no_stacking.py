# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Requirement 2.4: no discount combines with dealer pricing, toggle default off."""

import json
import sys
import types
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from prices import Money

from ....checkout.base_calculations import (
    calculate_base_line_total_price,
    checkout_total,
    get_line_total_price_with_propagated_checkout_discount,
)
from ....checkout.complete_checkout import create_order_from_checkout
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....checkout.utils import add_voucher_to_checkout
from ....discount import VoucherType
from ....order.calculations import fetch_order_prices_if_expired
from ....plugins.manager import get_plugins_manager
from ....product.models import ProductVariant, ProductVariantChannelListing
from .. import no_stacking
from ..models import DealerSettings
from ..no_stacking import LINE_METADATA_KEY, PRICE_OVERRIDE_REASON, catalogue_guard

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_toggle_cache():
    no_stacking.reset_cache()
    yield
    no_stacking.reset_cache()


@pytest.fixture
def retail_variant(product, channel_USD):
    variant = ProductVariant.objects.create(product=product, sku="SKU_RETAIL")
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel_USD,
        price_amount=Decimal(10),
        discounted_price_amount=Decimal(10),
        currency=channel_USD.currency_code,
    )
    return variant


@pytest.fixture
def checkout_with_a_dealer_line_and_a_retail_line(
    checkout, variant, retail_variant, voucher_percentage, channel_USD
):
    """One line at a dealer tier of 8.00, one at retail 10.00, a 10% voucher.

    SPECIFIC_PRODUCT with no product restriction means every line qualifies,
    which is the shape that makes the assertion about WHICH line got discounted
    meaningful.
    """
    dealer_line = CheckoutLine.objects.create(
        checkout=checkout,
        variant=variant,
        quantity=1,
        currency=channel_USD.currency_code,
        price_override=Decimal("8.00"),
        price_override_reason=PRICE_OVERRIDE_REASON,
        undiscounted_unit_price_amount=Decimal("8.00"),
        private_metadata={
            LINE_METADATA_KEY: json.dumps({"group": "dealer-1", "minQuantity": 1})
        },
    )
    retail_line = CheckoutLine.objects.create(
        checkout=checkout,
        variant=retail_variant,
        quantity=1,
        currency=channel_USD.currency_code,
        undiscounted_unit_price_amount=Decimal("10.00"),
    )

    voucher_percentage.type = VoucherType.SPECIFIC_PRODUCT
    voucher_percentage.save(update_fields=["type"])
    checkout.voucher_code = voucher_percentage.codes.first().code
    checkout.save(update_fields=["voucher_code"])
    return checkout, dealer_line, retail_line


def totals(checkout):
    lines_info, _ = fetch_checkout_lines(checkout)
    return {
        info.line.pk: calculate_base_line_total_price(info) for info in lines_info
    }


def test_a_voucher_skips_the_dealer_line_when_stacking_is_off(
    checkout_with_a_dealer_line_and_a_retail_line,
):
    checkout, dealer_line, retail_line = checkout_with_a_dealer_line_and_a_retail_line

    by_line = totals(checkout)

    # The dealer line pays its tier and nothing else; the retail line takes the
    # full 10 percent. Without the guard the dealer line would come to 7.20.
    assert by_line[dealer_line.pk] == Money(Decimal("8.00"), "USD")
    assert by_line[retail_line.pk] == Money(Decimal("9.00"), "USD")


def test_the_same_voucher_stacks_once_the_merchant_turns_stacking_on(
    checkout_with_a_dealer_line_and_a_retail_line,
):
    checkout, dealer_line, retail_line = checkout_with_a_dealer_line_and_a_retail_line
    DealerSettings.objects.create(discount_stacking=True)

    by_line = totals(checkout)

    assert by_line[dealer_line.pk] == Money(Decimal("7.20"), "USD")
    assert by_line[retail_line.pk] == Money(Decimal("9.00"), "USD")


def test_no_row_means_the_toggle_is_off(db):
    assert DealerSettings.stacking_enabled() is False


class _Line:
    def __init__(self, stamps, discounts=()):
        self.private_metadata = stamps
        self._discounts = list(discounts)


class _LineInfo:
    def __init__(self, line):
        self.line = line

    def get_catalogue_discounts(self):
        return self.line._discounts


def test_catalogue_promotions_are_offered_only_the_retail_lines(db):
    """The promotion guard, against a recording stand-in for the stock function.

    The stock function's behaviour is Saleor's business; what can be wrong here
    is WHICH lines reach it, and that a promotion already written onto a line
    that later became a dealer line is taken back off.
    """
    seen = []

    def original(lines_info):
        seen.append(lines_info)
        return ([], [], [], [], None)

    dealer = _LineInfo(_Line({LINE_METADATA_KEY: "{}"}, discounts=["stale"]))
    retail = _LineInfo(_Line({}))

    creates, updates, removes, fields, end = catalogue_guard(original)([dealer, retail])

    assert seen == [[retail]]
    assert removes == ["stale"]

    # Toggle on and the stock function gets every line back, untouched.
    DealerSettings.objects.create(discount_stacking=True)
    no_stacking.reset_cache()
    catalogue_guard(original)([dealer, retail])
    assert seen[-1] == [dealer, retail]


# ---------------------------------------------------------------------------
# MP2: the ENTIRE_ORDER voucher, which is not a line-level discount at all.
#
# The exploit these four close: a 10 percent ENTIRE_ORDER voucher on a checkout
# holding a 3400.00 dealer line and a 6399.00 retail line. Stock Saleor sizes the
# discount on the whole 9799.00 (979.90) and then spreads it in proportion to each
# line's share, so 340.00 of it lands on the dealer line, on top of the tier the
# merchant already granted. MP1 does not see this: the voucher never reaches a
# line, so there is no line voucher to clear.
# ---------------------------------------------------------------------------

DEALER_UNIT = Decimal("3400.00")
RETAIL_UNIT = Decimal("6399.00")
USD = "USD"


@pytest.fixture
def big_retail_variant(product, channel_USD):
    """A retail line worth enough that the split is readable in whole cents."""
    variant = ProductVariant.objects.create(
        product=product, sku="SKU_RETAIL_BIG", track_inventory=False
    )
    ProductVariantChannelListing.objects.create(
        variant=variant,
        channel=channel_USD,
        price_amount=RETAIL_UNIT,
        discounted_price_amount=RETAIL_UNIT,
        currency=channel_USD.currency_code,
    )
    return variant


@pytest.fixture
def entire_order_checkout(
    checkout, variant, big_retail_variant, voucher_percentage, channel_USD, address
):
    """Build the checkout, with the dealer stamp on the first line or without it.

    voucher_percentage is ENTIRE_ORDER at 10 percent as it comes, which is the
    type MP1 leaves alone.
    """
    variant.track_inventory = False
    variant.save(update_fields=["track_inventory"])

    def build(dealer: bool):
        dealer_line = CheckoutLine.objects.create(
            checkout=checkout,
            variant=variant,
            quantity=1,
            currency=channel_USD.currency_code,
            price_override=DEALER_UNIT,
            price_override_reason=PRICE_OVERRIDE_REASON,
            undiscounted_unit_price_amount=DEALER_UNIT,
            private_metadata=(
                {
                    LINE_METADATA_KEY: json.dumps(
                        {"group": "dealer-1", "minQuantity": 1}
                    )
                }
                if dealer
                else {}
            ),
        )
        retail_line = CheckoutLine.objects.create(
            checkout=checkout,
            variant=big_retail_variant,
            quantity=1,
            currency=channel_USD.currency_code,
            undiscounted_unit_price_amount=RETAIL_UNIT,
        )
        checkout.billing_address = address
        checkout.shipping_address = address
        checkout.redirect_url = "https://www.example.com"
        checkout.save()

        manager = get_plugins_manager(allow_replica=False)
        lines_info, _ = fetch_checkout_lines(checkout)
        checkout_info = fetch_checkout_info(checkout, lines_info, manager)
        add_voucher_to_checkout(
            manager,
            checkout_info,
            lines_info,
            voucher_percentage,
            voucher_percentage.codes.first(),
        )
        return manager, checkout_info, lines_info, dealer_line, retail_line

    return build


def propagated(checkout_info, lines_info):
    return {
        info.line.pk: get_line_total_price_with_propagated_checkout_discount(
            checkout_info, lines_info, info
        )
        for info in lines_info
    }


def test_an_entire_order_voucher_leaves_the_dealer_line_whole(entire_order_checkout):
    manager, checkout_info, lines_info, dealer_line, retail_line = entire_order_checkout(
        dealer=True
    )

    # Sized on the retail line alone: 10 percent of 6399.00, not of 9799.00.
    assert checkout_info.checkout.discount == Money(Decimal("639.90"), USD)

    by_line = propagated(checkout_info, lines_info)
    assert by_line[dealer_line.pk] == Money(DEALER_UNIT, USD)
    assert by_line[retail_line.pk] == Money(Decimal("5759.10"), USD)
    assert checkout_total(checkout_info, lines_info) == Money(
        Decimal("9159.10"), USD
    )


def test_the_same_checkout_without_a_dealer_line_takes_the_stock_split(
    entire_order_checkout,
):
    """The control. Same two lines, same voucher, no dealer stamp."""
    manager, checkout_info, lines_info, first_line, retail_line = entire_order_checkout(
        dealer=False
    )

    assert checkout_info.checkout.discount == Money(Decimal("979.90"), USD)

    by_line = propagated(checkout_info, lines_info)
    assert by_line[first_line.pk] == Money(Decimal("3060.00"), USD)
    assert by_line[retail_line.pk] == Money(Decimal("5759.10"), USD)
    assert checkout_total(checkout_info, lines_info) == Money(
        Decimal("8819.10"), USD
    )


def test_the_merchant_can_turn_the_entire_order_stacking_back_on(
    entire_order_checkout,
):
    DealerSettings.objects.create(discount_stacking=True)
    no_stacking.reset_cache()

    manager, checkout_info, lines_info, dealer_line, retail_line = entire_order_checkout(
        dealer=True
    )

    by_line = propagated(checkout_info, lines_info)
    assert by_line[dealer_line.pk] == Money(Decimal("3060.00"), USD)
    assert by_line[retail_line.pk] == Money(Decimal("5759.10"), USD)


def test_the_order_carries_the_same_numbers(entire_order_checkout, app):
    """checkoutComplete, then the price refresh every read of the order goes through.

    The order is where the second pair of wrappers earns its place: the checkout
    spread does not carry into the order, the order recomputes the voucher amount
    from its own subtotal and re-spreads it over its own lines on every refresh.
    This is the number the merchant sees in the dashboard and the customer sees on
    the invoice.
    """
    manager, checkout_info, lines_info, dealer_line, retail_line = entire_order_checkout(
        dealer=True
    )

    order = create_order_from_checkout(
        checkout_info=checkout_info, manager=manager, user=None, app=app
    )

    order, refreshed_lines = fetch_order_prices_if_expired(
        order, manager, None, force_update=True
    ).get()

    order_lines = list(refreshed_lines)
    by_sku = {line.product_sku: line for line in order_lines}
    dealer_order_line = by_sku[dealer_line.variant.sku]
    retail_order_line = by_sku[retail_line.variant.sku]

    assert dealer_order_line.total_price_net == Money(DEALER_UNIT, USD)
    assert retail_order_line.total_price_net == Money(Decimal("5759.10"), USD)
    assert order.total_net == Money(Decimal("9159.10"), USD)

    # The order-level discount record the merchant and the invoice both read.
    discount = order.discounts.get()
    assert discount.amount == Money(Decimal("639.90"), USD)

    # And the same numbers once they have been through the database.
    dealer_order_line.refresh_from_db()
    retail_order_line.refresh_from_db()
    assert dealer_order_line.total_price_net == Money(DEALER_UNIT, USD)
    assert retail_order_line.total_price_net == Money(Decimal("5759.10"), USD)



# --- finding 12: the binding sites are discovered, not hoped for -------------


def test_the_pinned_binding_sites_are_exactly_the_discovered_ones():
    """An upstream bump that adds an import site reddens here and at boot.

    `saleor.checkout.fetch` is deliberately absent: it imports the function
    INSIDE the function that uses it, so it resolves through the defining module
    at call time and there is no module attribute to rebind.
    """
    guard = no_stacking.installed_voucher_guard()

    assert guard is not None, "MP1 was never installed"
    assert no_stacking.binding_sites(guard) == frozenset(
        no_stacking.VOUCHER_BINDING_SITES
    )


def test_the_catalogue_guard_goes_in_through_the_same_pinned_machinery():
    """It used to be rebound by hand on the defining module and nowhere else.

    A hand-rebind cannot fail: it patches the one module it names and says
    nothing about the ones it does not, so an upstream bump that imports this
    function somewhere new leaves the guard standing in at some call sites and
    not at others, and a catalogue promotion stacks on a dealer price at the
    sites it missed. Going through `install_guard` makes that a boot error.
    """
    guard = no_stacking.installed_catalogue_guard()

    assert guard is not None, "MP1's catalogue half was not installed through the pin"
    assert no_stacking.binding_sites(guard) == frozenset(
        no_stacking.CATALOGUE_BINDING_SITES
    )


def test_an_unpinned_catalogue_binding_site_refuses_to_boot(monkeypatch):
    """What the hand-rebind could not do: notice, and stop."""
    guard = no_stacking.installed_catalogue_guard()
    newcomer = types.ModuleType("saleor.discount.utils.somewhere_new")
    newcomer.prepare_checkout_line_discount_objects_for_catalogue_promotions = guard
    monkeypatch.setitem(sys.modules, "saleor.discount.utils.somewhere_new", newcomer)

    with pytest.raises(ImproperlyConfigured):
        no_stacking._guard_catalogue_promotions()


def test_a_new_binding_site_is_discovered(monkeypatch):
    """The mechanism that makes the pin above meaningful: discovery is live."""
    guard = no_stacking.installed_voucher_guard()
    newcomer = types.ModuleType("saleor.checkout.somewhere_new")
    newcomer.attach_voucher_to_line_info = guard
    monkeypatch.setitem(sys.modules, "saleor.checkout.somewhere_new", newcomer)

    discovered = no_stacking.binding_sites(guard)

    assert "saleor.checkout.somewhere_new" in discovered
    assert discovered != frozenset(no_stacking.VOUCHER_BINDING_SITES)
