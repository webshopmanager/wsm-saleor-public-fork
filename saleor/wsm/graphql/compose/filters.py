# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The three list filters the Compose screens need, and nothing else.

`product` and `search` on every list, `prop65` on the compliance audit view.
No `sortBy` in v1: every list comes back in the model's own `Meta.ordering`,
which is the order the admin showed, and adding a sorter later is additive and
breaks no saved document.

The search is the admin's ranked SKU search, imported from
`saleor.wsm.compose.search` rather than copied, because a merchant typing a part
number into the Dashboard and into the console is asking the same question.
"""

import django_filters

from ....graphql.core.filters import FilterInputObjectType, GlobalIDFilter
from ....graphql.utils import resolve_global_ids_to_primary_keys
from ...compose import models
from ...compose.search import search_and_rank
from ..types import WsmDocCategory


def _product_pk(value):
    _type, pks = resolve_global_ids_to_primary_keys([value], "Product")
    return pks[0]


def filter_product(qs, _name, value):
    if not value:
        return qs
    return qs.filter(product_id=_product_pk(value))


def filter_option_set_search(qs, _name, value):
    return search_and_rank(
        qs, value, product_path="product", name_fields=("name", "label")
    )


def filter_fee_search(qs, _name, value):
    return search_and_rank(
        qs, value, product_path="product", name_fields=("label", "sku")
    )


def filter_compliance_search(qs, _name, value):
    # No merchant-readable name of its own: a compliance row IS its product, so
    # the product's own name and SKUs are the only honest thing to search.
    return search_and_rank(qs, value, product_path="product", name_fields=())


class WsmOptionSetFilter(django_filters.FilterSet):
    product = GlobalIDFilter(method=filter_product)
    search = django_filters.CharFilter(method=filter_option_set_search)

    class Meta:
        model = models.OptionSet
        fields = ["product", "search"]


class WsmFeeFilter(django_filters.FilterSet):
    product = GlobalIDFilter(method=filter_product)
    search = django_filters.CharFilter(method=filter_fee_search)

    class Meta:
        model = models.Fee
        fields = ["product", "search"]


class WsmProductComplianceFilter(django_filters.FilterSet):
    product = GlobalIDFilter(method=filter_product)
    prop65 = django_filters.BooleanFilter(field_name="prop65")
    search = django_filters.CharFilter(method=filter_compliance_search)

    class Meta:
        model = models.ProductCompliance
        fields = ["product", "prop65", "search"]


class WsmOptionSetFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = WsmOptionSetFilter


class WsmFeeFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = WsmFeeFilter


class WsmProductComplianceFilterInput(WsmDocCategory, FilterInputObjectType):
    class Meta:
        filterset_class = WsmProductComplianceFilter
