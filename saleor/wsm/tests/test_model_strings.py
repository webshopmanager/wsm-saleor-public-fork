# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What every fork row calls itself, on the page that asks before deleting it.

`__str__` is not decoration here. It is the heading of the change page, the
breadcrumb, the history, the picker result and, the one that matters, the
"Are you sure?" on a delete. The merchant walk of 2026-09-08 found a tier price
introducing itself as "7 x1: 228.000": a DealerGroup row id the merchant has
never seen, no product, no SKU, and a third decimal place.
"""

from decimal import Decimal

import pytest

from ..compose import pricing
from ..compose.models import Fee, OptionSet, OptionValue
from ..containers.models import EXCLUDES, KitConfig, KitMember, KitMemberRule, SeriesConfig
from ..dealer.models import DealerCustomer, DealerGroup, TierPrice

pytestmark = pytest.mark.django_db


@pytest.fixture
def rows(product, variant, collection, customer_user):
    """One of every fork row a merchant can reach a delete confirmation for."""
    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    kit = KitConfig.objects.create(collection=collection)
    member = KitMember.objects.create(kit=kit, variant=variant, quantity=2)
    return {
        "group": group,
        "customer": DealerCustomer.objects.create(user=customer_user, group=group),
        "tier": TierPrice.objects.create(
            variant=variant, group=group, min_quantity=1, amount=Decimal("228.000")
        ),
        "set": option_set,
        "value": OptionValue.objects.create(option_set=option_set, name="Black"),
        "fee": Fee.objects.create(
            product=product,
            label="Freight crating",
            basis=pricing.FIXED,
            amount=Decimal("149"),
        ),
        "series": SeriesConfig.objects.create(collection=collection),
        "kit": kit,
        "member": member,
        "rule": KitMemberRule.objects.create(
            kit=kit, subject=member, kind=EXCLUDES, message="never both"
        ),
    }


def test_each_row_says_what_it_is(rows, product, variant, collection, customer_user):
    assert str(rows["group"]) == "Dealer 1 (dealer-1)"
    assert str(rows["customer"]) == f"{customer_user.email} in Dealer 1 (dealer-1)"
    assert str(rows["tier"]) == (
        f"{variant.sku} - Dealer 1 (dealer-1) at qty 1: 228.00"
    )
    assert str(rows["set"]) == f"{product.name}: Colour"
    assert str(rows["value"]) == f"Black ({product.name}: Colour)"
    assert str(rows["fee"]) == f"Freight crating on {product.name}"
    assert str(rows["series"]) == f"Series: {collection.name}"
    assert str(rows["kit"]) == f"Kit: {collection.name}"
    assert str(rows["member"]) == f"2 x {product.name} [{variant.sku}]"
    assert str(rows["rule"]) == (
        f"{product.name} [{variant.sku}] cannot be sold with"
    )


def test_no_row_introduces_itself_by_a_number(rows):
    """The class of defect, not the nine instances of it.

    A string that is a row id, or that carries one, names something the merchant
    has never been shown. A new fork model whose `__str__` does it fails here.
    """
    for name, row in rows.items():
        shown = str(row)
        assert shown, name
        assert str(row.pk) != shown, name
        for field in row._meta.fields:
            if not field.is_relation:
                continue
            related_id = getattr(row, field.attname)
            if related_id is None or not isinstance(related_id, int):
                continue
            assert str(related_id) not in shown.split(), (name, field.name)
