# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What the three dealer lists can be narrowed by, and nothing more.

The filters are the columns the admin's list screens already filtered on
(`dealer/admin.py`), because those are the questions a merchant asks of a list:
which group, is this one tax exempt, which product, and the free-text box. No
sorter in v1: every list comes back in the model's own `Meta.ordering`, which is
the order the admin showed, and adding one later breaks no document.
"""

import django_filters
from django.db.models import Q

from ....graphql.core.filters import GlobalIDFilter
from ....graphql.core.filters.filter_input import FilterInputObjectType
from ...dealer import models
from ..types import WsmDocCategory


def filter_search(fields):
    """One substring over the fields a merchant would type into a search box."""

    def search(qs, _name, value):
        if not value:
            return qs
        query = Q()
        for field in fields:
            query |= Q(**{f"{field}__icontains": value})
        return qs.filter(query)

    return search


class DealerGroupFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method=filter_search(["code", "name"]))

    class Meta:
        model = models.DealerGroup
        fields: list[str] = []


class DealerCustomerFilter(django_filters.FilterSet):
    group = GlobalIDFilter(field_name="group")
    tax_exempt = django_filters.BooleanFilter(field_name="tax_exempt")
    search = django_filters.CharFilter(
        method=filter_search(
            ["user__email", "user__first_name", "user__last_name", "group__code"]
        )
    )

    class Meta:
        model = models.DealerCustomer
        fields = ["group", "tax_exempt"]


class TierPriceFilter(django_filters.FilterSet):
    group = GlobalIDFilter(field_name="group")
    variant = GlobalIDFilter(field_name="variant")
    # The group detail screen lists every break on one PRODUCT at a time, and a
    # product is many variants: filtering by variant alone would make the
    # merchant page through a SKU at a time.
    product = GlobalIDFilter(field_name="variant__product")
    search = django_filters.CharFilter(
        method=filter_search(["variant__sku", "variant__product__name"])
    )

    class Meta:
        model = models.TierPrice
        fields = ["group", "variant", "product"]


class WsmDealerGroupFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = DealerGroupFilter


class WsmDealerCustomerFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = DealerCustomerFilter


class WsmTierPriceFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = TierPriceFilter
