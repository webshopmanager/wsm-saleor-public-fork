# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The gate rule itself, and the two places it refuses a purchase.

The rule is one function, so it is tested as one: every row of the table below
is a sentence from the requirements, and there is nowhere else for the answer to
come from. The enforcement tests go through the real functions the mutations
call, not through a resolver by hand, because the thing being proved is that a
line cannot be WRITTEN, not that a helper returns False.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from ....graphql.checkout.mutations.utils import CheckoutLineData
from ..gate import (
    GATED_CODE,
    Gate,
    blocked_products,
    buyer_groups_for,
    category_gates_for_products,
    gates_for_products,
    is_gated,
    refuse_gated_lines,
)
from ..models import (
    DealerCategoryGate,
    DealerCustomer,
    DealerGroup,
    DealerProductGate,
    DealerSettings,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def fob():
    return DealerGroup.objects.create(code="fob", name="FOB")


@pytest.fixture
def cif():
    return DealerGroup.objects.create(code="cif", name="CIF")


@pytest.fixture
def fob_buyer(customer_user, fob):
    DealerCustomer.objects.create(user=customer_user, group=fob)
    return customer_user


# (gate, site_gated, buyer_group) -> gated?
#
# Every way through the rule, named. The three that matter to ds are the first
# row (the public on a gated store), the fifth (a dealer on a gated store) and
# the last two (a part one trade tier may see and another may not).
NOBODY = frozenset()
FOB = frozenset({"fob"})
CIF = frozenset({"cif"})
BOTH = frozenset({"fob", "cif"})


@pytest.mark.parametrize(
    ("gate", "site_gated", "buyer_groups", "expected"),
    [
        (None, True, NOBODY, True),
        (None, False, NOBODY, False),
        (None, False, FOB, False),
        (None, True, FOB, False),
        (Gate(True, frozenset()), False, NOBODY, True),
        (Gate(True, frozenset()), False, FOB, False),
        (Gate(False, frozenset()), True, NOBODY, False),
        (Gate(True, FOB), True, FOB, False),
        (Gate(True, FOB), True, CIF, True),
        (Gate(True, FOB), False, NOBODY, True),
        # Access is the UNION: one matching group is enough, and a buyer in
        # neither named group is still refused (Dana, 2026-09-11).
        (Gate(True, FOB), True, BOTH, False),
        (Gate(True, frozenset({"wd-pallet", "jobber"})), True, CIF, True),
        (
            Gate(True, frozenset({"wd-pallet", "jobber"})),
            True,
            frozenset({"cif", "jobber"}),
            False,
        ),
    ],
)
def test_the_rule(gate, site_gated, buyer_groups, expected):
    assert is_gated(gate, site_gated=site_gated, buyer_groups=buyer_groups) is expected


def test_an_unconfigured_store_gates_nothing():
    """An unconfigured store shows what it always showed.

    Fail SAFE means show LESS, and on a store nobody configured, less is what
    the store already showed: a filter that started hiding prices by itself
    would be the failure, not the fix.
    """
    assert DealerSettings.catalogue_gated_enabled() is False
    assert is_gated(None, site_gated=False, buyer_groups=frozenset()) is False


def test_gates_for_products_reads_the_groups_it_was_given(product, fob):
    gate = DealerProductGate.objects.create(product=product, login_required=True)
    gate.groups.add(fob)

    found = gates_for_products([product.pk])

    assert found == {product.pk: Gate(True, frozenset({"fob"}))}


def test_gates_for_products_is_empty_when_no_product_has_a_row(product):
    assert gates_for_products([product.pk]) == {}


def test_blocked_products_names_the_gated_one(product, fob_buyer, cif):
    DealerSettings.objects.create(catalogue_gated=True)

    assert blocked_products([product.pk], None) == {product.pk}
    assert blocked_products([product.pk], fob_buyer.pk) == set()


def test_blocked_products_scopes_by_group(product, fob_buyer, cif, customer_user2):
    DealerCustomer.objects.create(user=customer_user2, group=cif)
    gate = DealerProductGate.objects.create(product=product, login_required=True)
    gate.groups.add(DealerGroup.objects.get(code="fob"))

    assert blocked_products([product.pk], fob_buyer.pk) == set()
    assert blocked_products([product.pk], customer_user2.pk) == {product.pk}


def test_the_ds_shape_two_groups_see_a_part_and_a_third_does_not(
    product, customer_user, customer_user2
):
    """A part two trade tiers may see and a third may not.

    ds's real data, measured 2026-09-11: `customer_group_access_link` carries
    2,748 rows across 3 of its 5 groups (WD Pallet 1,371, Jobber 1,370,
    Container 7), while FOB and CIF carry none and are pure price books. So a
    part named to two groups and not a third is the common case, not the edge.
    """
    wd = DealerGroup.objects.create(code="wd-pallet", name="WD Pallet")
    jobber = DealerGroup.objects.create(code="jobber", name="Jobber")
    container = DealerGroup.objects.create(code="container", name="Container")
    DealerCustomer.objects.create(user=customer_user, group=jobber)
    DealerCustomer.objects.create(user=customer_user2, group=container)
    DealerSettings.objects.create(catalogue_gated=True)
    gate = DealerProductGate.objects.create(product=product, login_required=True)
    gate.groups.add(wd, jobber)

    assert blocked_products([product.pk], customer_user.pk) == set()
    assert blocked_products([product.pk], customer_user2.pk) == {product.pk}
    assert blocked_products([product.pk], None) == {product.pk}


def _line(variant, quantity):
    return CheckoutLineData(variant_id=str(variant.pk), quantity=quantity)


def test_a_cart_write_of_a_gated_variant_is_refused(checkout, variant):
    DealerSettings.objects.create(catalogue_gated=True)

    with pytest.raises(ValidationError) as refused:
        refuse_gated_lines(checkout, [variant], [_line(variant, 1)])

    assert variant.sku in refused.value.messages[0]


def test_a_member_may_write_the_same_line(checkout, variant, fob_buyer):
    DealerSettings.objects.create(catalogue_gated=True)
    checkout.user = fob_buyer
    checkout.save(update_fields=["user"])

    refuse_gated_lines(checkout, [variant], [_line(variant, 1)])


def test_removing_a_gated_line_is_never_refused(checkout, variant):
    """A gated line can always be taken back out of a cart.

    A shopper whose cart holds a line the merchant has since gated must still
    be able to remove it, and a removal is quantity 0 through the same function
    an add goes through.
    """
    DealerSettings.objects.create(catalogue_gated=True)

    refuse_gated_lines(checkout, [variant], [_line(variant, 0)])


def test_the_order_backstop_refuses_a_gated_line(checkout_with_item):
    """Whatever put the line in the cart, it does not become an order."""
    from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
    from ....plugins.manager import get_plugins_manager
    from ..plugin import DealerGatePlugin

    DealerSettings.objects.create(catalogue_gated=True)
    lines, _ = fetch_checkout_lines(checkout_with_item)
    manager = get_plugins_manager(allow_replica=False)
    info = fetch_checkout_info(checkout_with_item, lines, manager)

    with pytest.raises(ValidationError):
        DealerGatePlugin(configuration=[], active=True).preprocess_order_creation(
            info, lines, None
        )


def test_the_order_backstop_lets_a_member_through(checkout_with_item, fob_buyer):
    from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
    from ....plugins.manager import get_plugins_manager
    from ..plugin import DealerGatePlugin

    DealerSettings.objects.create(catalogue_gated=True)
    checkout_with_item.user = fob_buyer
    checkout_with_item.save(update_fields=["user"])
    lines, _ = fetch_checkout_lines(checkout_with_item)
    manager = get_plugins_manager(allow_replica=False)
    info = fetch_checkout_info(checkout_with_item, lines, manager)

    assert (
        DealerGatePlugin(configuration=[], active=True).preprocess_order_creation(
            info, lines, "unchanged"
        )
        == "unchanged"
    )


def test_the_refusal_carries_the_wsm_code():
    from ..gate import refusal

    assert refusal().code == GATED_CODE


def test_a_gated_store_still_prices_for_a_dealer_with_a_tier(
    product, variant, fob_buyer, channel_USD
):
    """The gate hides prices from the public; it must not touch the dealer's."""
    from ..models import TierPrice
    from ..pricing import dealer_price_for

    TierPrice.objects.create(
        variant=variant,
        group=DealerGroup.objects.get(code="fob"),
        min_quantity=1,
        amount=Decimal("5.00"),
    )
    DealerSettings.objects.create(catalogue_gated=True)

    found = dealer_price_for(variant, fob_buyer, 1, channel=channel_USD)

    assert found is not None
    assert found.amount == Decimal("5.00")


# --------------------------------------------------------------- CATEGORIES
#
# 5.0 gates SECTIONS as well as products: 76 tenants login-gate a category or
# page (127 rows) and 40 scope content by group (210 rows). ds alone carries 15
# category visibility rows and 5 category login gates, every one of which a
# product-only gate dropped on the floor.


def test_a_category_gate_covers_the_products_in_it(product, category):
    DealerCategoryGate.objects.create(category=category, login_required=True)

    assert product.category_id == category.pk
    assert blocked_products([product.pk], None) == {product.pk}


def test_a_category_gate_covers_a_product_in_a_CHILD_category(product, category):
    """A merchant gating a section means the section, not one level of it."""
    from ....product.models import Category

    child = Category.objects.create(name="Inner", slug="inner", parent=category)
    product.category = child
    product.save(update_fields=["category"])
    DealerCategoryGate.objects.create(category=category, login_required=True)

    assert blocked_products([product.pk], None) == {product.pk}


def test_a_products_own_row_beats_its_category(product, category):
    """Most specific wins, and it wins in the PERMISSIVE direction too."""
    DealerCategoryGate.objects.create(category=category, login_required=True)
    DealerProductGate.objects.create(product=product, login_required=False)

    assert blocked_products([product.pk], None) == set()


def test_the_nearest_gated_ancestor_wins(product, category, fob, cif, customer_user):
    """Two gated ancestors, and the deeper one decides."""
    from ....product.models import Category

    child = Category.objects.create(name="Inner", slug="inner2", parent=category)
    product.category = child
    product.save(update_fields=["category"])
    outer = DealerCategoryGate.objects.create(category=category, login_required=True)
    outer.groups.add(cif)
    inner = DealerCategoryGate.objects.create(category=child, login_required=True)
    inner.groups.add(fob)

    # The buyer is in FOB, which the INNER gate names and the outer one does not.
    DealerCustomer.objects.create(user=customer_user, group=fob)

    assert blocked_products([product.pk], customer_user.pk) == set()


def test_a_category_gate_scoped_to_a_group_hides_it_from_another(
    product, category, fob_buyer, cif, customer_user2
):
    DealerCustomer.objects.create(user=customer_user2, group=cif)
    gate = DealerCategoryGate.objects.create(category=category, login_required=True)
    gate.groups.add(DealerGroup.objects.get(code="fob"))

    assert blocked_products([product.pk], fob_buyer.pk) == set()
    assert blocked_products([product.pk], customer_user2.pk) == {product.pk}


def test_a_public_category_gate_publishes_a_section_of_a_gated_store(product, category):
    DealerSettings.objects.create(catalogue_gated=True)
    DealerCategoryGate.objects.create(category=category, login_required=False)

    assert blocked_products([product.pk], None) == set()


def test_no_category_gate_costs_nothing(product, django_assert_num_queries):
    """The store that has gated no section must not pay for the tree."""
    with django_assert_num_queries(1):
        assert category_gates_for_products([product.pk]) == {}


# ------------------------------------------------------------- MULTI-GROUP


def test_access_is_the_union_of_every_group_the_buyer_is_in(
    product, category, customer_user, fob, cif
):
    """Dana, 2026-09-11: ACCESS is the union, PRICE stays the one group.

    5.0's `customer_group_link` is many-to-many on 28 tenants and 2,533
    customers; ds has 156 customers in the link table against 96 on the legacy
    single-group column.
    """
    wd = DealerGroup.objects.create(code="wd-pallet", name="WD Pallet")
    dealer = DealerCustomer.objects.create(user=customer_user, group=cif)
    dealer.access_groups.add(wd)
    gate = DealerProductGate.objects.create(product=product, login_required=True)
    gate.groups.add(wd)

    assert buyer_groups_for(customer_user.pk) == frozenset({"cif", "wd-pallet"})
    assert blocked_products([product.pk], customer_user.pk) == set()


def test_the_price_group_is_still_exactly_one(customer_user, fob, cif):
    """The union is visibility only: `tier_group_for` answers one group."""
    from ..pricing import tier_group_for

    dealer = DealerCustomer.objects.create(user=customer_user, group=cif)
    dealer.access_groups.add(fob)

    assert tier_group_for(customer_user.pk) == "cif"


def test_a_buyer_in_no_group_has_no_access_groups(customer_user):
    assert buyer_groups_for(customer_user.pk) == frozenset()
    assert buyer_groups_for(None) == frozenset()
