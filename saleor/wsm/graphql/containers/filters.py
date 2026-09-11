# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What the two container lists can be narrowed by, and nothing more.

The filters are the columns the admin's list screens already filtered on
(`containers/admin.py`), because those are the questions a merchant asks of a
list: which collection, is it live, and the free-text box. No sorter in v1:
every list comes back in the model's own `Meta.ordering`, which is the order the
admin showed, and adding one later breaks no document.
"""

import django_filters
from django.db.models import Q

from ....graphql.core.filters import GlobalIDFilter
from ....graphql.core.filters.filter_input import FilterInputObjectType
from ...containers import models
from ..types import DOC_CATEGORY_WSM


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


class SeriesConfigFilter(django_filters.FilterSet):
    collection = GlobalIDFilter(field_name="collection")
    brand = django_filters.CharFilter(field_name="brand", lookup_expr="iexact")
    published = django_filters.BooleanFilter(field_name="published")
    search = django_filters.CharFilter(
        method=filter_search(["brand", "collection__name"])
    )

    class Meta:
        model = models.SeriesConfig
        fields = ["collection", "brand", "published"]


class KitConfigFilter(django_filters.FilterSet):
    collection = GlobalIDFilter(field_name="collection")
    active = django_filters.BooleanFilter(field_name="active")
    search = django_filters.CharFilter(method=filter_search(["collection__name"]))

    class Meta:
        model = models.KitConfig
        fields = ["collection", "active"]


class WsmSeriesConfigFilterInput(FilterInputObjectType):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        filterset_class = SeriesConfigFilter


class WsmKitConfigFilterInput(FilterInputObjectType):
    class Meta:
        doc_category = DOC_CATEGORY_WSM
        filterset_class = KitConfigFilter
