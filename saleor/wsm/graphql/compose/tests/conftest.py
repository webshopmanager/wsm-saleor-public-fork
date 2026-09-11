# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What the Compose GraphQL tests share: a merchant, and one row of each shape.

Every test in this package posts to `reverse("api")`, which on this branch is
the composed schema `saleor/wsm/urls.py` mounts. A resolver called by hand
cannot tell the difference between a field that exists and a field that is
SERVED, and a permission gate is only real over the wire.
"""

from decimal import Decimal

import pytest

from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    Fee,
    OptionSet,
    OptionValue,
    ProductCompliance,
)
from saleor.wsm.dealer.models import DealerGroup


@pytest.fixture
def merchant_api_client(staff_api_client, permission_manage_products):
    """Staff WITH the permission, granted once and standing.

    `post_graphql(permissions=...)` asserts the call is refused BEFORE it
    grants, which can only be true on the first call; a test that mutates twice
    needs the grant already in place.
    """
    staff_api_client.user.user_permissions.add(permission_manage_products)
    return staff_api_client


@pytest.fixture
def dealer_group(db):
    """A tier row names a group by CODE, so the code has to name a real group."""
    return DealerGroup.objects.create(code="dealer-1", name="Dealer tier 1")


@pytest.fixture
def option_set(product):
    """One question with two answers: a surcharge and a small credit."""
    row = OptionSet.objects.create(
        product=product,
        name="Finish",
        label="Choose a finish",
        prompt_type=pricing.CHOICE_ONE,
        sort_order=1,
    )
    OptionValue.objects.create(
        option_set=row,
        name="Black",
        sku_fragment="BLK",
        price_delta=Decimal("10.00"),
        sort_order=0,
    )
    OptionValue.objects.create(
        option_set=row,
        name="Raw",
        sku_fragment="RAW",
        price_delta=Decimal("-1.00"),
        sort_order=1,
    )
    return row


@pytest.fixture
def black(option_set):
    return option_set.values.get(name="Black")


@pytest.fixture
def tier_delta(black, dealer_group):
    return DealerTierOptionPrice.objects.create(
        option_value=black, tier_group=dealer_group.code, price_delta=Decimal("5.00")
    )


@pytest.fixture
def fee(product):
    return Fee.objects.create(
        product=product,
        label="Freight crating",
        sku="CRATE",
        basis=pricing.FIXED,
        amount=Decimal("149.00"),
        apply_to=pricing.PER_LINE,
    )


@pytest.fixture
def compliance(product):
    return ProductCompliance.objects.create(
        product=product, prop65=True, restricted_states="CA"
    )
