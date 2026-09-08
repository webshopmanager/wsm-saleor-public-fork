# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Requirement 2.4: no discount combines with dealer pricing, toggle default off."""

import json
import sys
import types
from decimal import Decimal

import pytest
from prices import Money

from ....checkout.base_calculations import calculate_base_line_total_price
from ....checkout.fetch import fetch_checkout_lines
from ....checkout.models import CheckoutLine
from ....discount import VoucherType
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
        metadata={
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
    def __init__(self, metadata, discounts=()):
        self.metadata = metadata
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


def test_a_new_binding_site_is_discovered(monkeypatch):
    """The mechanism that makes the pin above meaningful: discovery is live."""
    guard = no_stacking.installed_voucher_guard()
    newcomer = types.ModuleType("saleor.checkout.somewhere_new")
    newcomer.attach_voucher_to_line_info = guard
    monkeypatch.setitem(sys.modules, "saleor.checkout.somewhere_new", newcomer)

    discovered = no_stacking.binding_sites(guard)

    assert "saleor.checkout.somewhere_new" in discovered
    assert discovered != frozenset(no_stacking.VOUCHER_BINDING_SITES)
