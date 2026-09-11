# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One query per relation per request, not one per row.

The Compose tab on the product detail page selects `wsmOptionSets { values {
tierDeltas } }` on a LIST of products. Resolved naively that is one query per
product, plus one per question, plus one per answer: a three-level N+1 on the
screen the merchant opens most. Every loader below is the stock
`saleor.graphql.core.dataloaders.DataLoader`, keyed the way stock's own
`CollectionsByProductIdLoader` is keyed, so the cost of that page is a constant
number of queries whatever the page size.

`test_the_compose_tab_costs_the_same_for_one_product_and_for_five` is what says
so, and it compares two counts rather than pinning one, because a pinned number
goes stale on the next upstream bump while "constant in N" is the property that
actually matters.
"""

from collections import defaultdict

from ....graphql.core.dataloaders import DataLoader
from ...compose import models


class OptionSetsByProductIdLoader(DataLoader):
    context_key = "wsm_option_sets_by_product"

    def batch_load(self, keys):
        rows = (
            models.OptionSet.objects.using(self.database_connection_name)
            .filter(product_id__in=keys)
            .order_by("sort_order", "pk")
        )
        by_product = defaultdict(list)
        for row in rows:
            by_product[row.product_id].append(row)
        return [by_product[key] for key in keys]


class OptionSetByIdLoader(DataLoader):
    context_key = "wsm_option_set_by_id"

    def batch_load(self, keys):
        rows = models.OptionSet.objects.using(self.database_connection_name).in_bulk(
            keys
        )
        return [rows.get(key) for key in keys]


class OptionValuesByOptionSetIdLoader(DataLoader):
    context_key = "wsm_option_values_by_option_set"

    def batch_load(self, keys):
        rows = (
            models.OptionValue.objects.using(self.database_connection_name)
            .filter(option_set_id__in=keys)
            .order_by("sort_order", "pk")
        )
        by_set = defaultdict(list)
        for row in rows:
            by_set[row.option_set_id].append(row)
        return [by_set[key] for key in keys]


class OptionValueByIdLoader(DataLoader):
    context_key = "wsm_option_value_by_id"

    def batch_load(self, keys):
        rows = models.OptionValue.objects.using(self.database_connection_name).in_bulk(
            keys
        )
        return [rows.get(key) for key in keys]


class TierDeltasByOptionValueIdLoader(DataLoader):
    context_key = "wsm_tier_deltas_by_option_value"

    def batch_load(self, keys):
        rows = (
            models.DealerTierOptionPrice.objects.using(self.database_connection_name)
            .filter(option_value_id__in=keys)
            .order_by("tier_group", "pk")
        )
        by_value = defaultdict(list)
        for row in rows:
            by_value[row.option_value_id].append(row)
        return [by_value[key] for key in keys]


class FeesByProductIdLoader(DataLoader):
    context_key = "wsm_fees_by_product"

    def batch_load(self, keys):
        rows = (
            models.Fee.objects.using(self.database_connection_name)
            .filter(product_id__in=keys)
            .order_by("pk")
        )
        by_product = defaultdict(list)
        for row in rows:
            by_product[row.product_id].append(row)
        return [by_product[key] for key in keys]


class ComplianceByProductIdLoader(DataLoader):
    context_key = "wsm_compliance_by_product"

    def batch_load(self, keys):
        rows = {
            row.product_id: row
            for row in models.ProductCompliance.objects.using(
                self.database_connection_name
            ).filter(product_id__in=keys)
        }
        return [rows.get(key) for key in keys]


class ShippingZoneIdsByComplianceIdLoader(DataLoader):
    """The through table only. The zones themselves come from stock's own loader.

    Two loaders rather than one because `ShippingZoneByIdLoader` already exists
    and already batches; asking it for ids we looked up is one more batch, never
    one more query per row.
    """

    context_key = "wsm_shipping_zone_ids_by_compliance"

    def batch_load(self, keys):
        through = models.ProductCompliance.include_shipping_zones.through
        pairs = (
            through.objects.using(self.database_connection_name)
            .filter(productcompliance_id__in=keys)
            .order_by("id")
            .values_list("productcompliance_id", "shippingzone_id")
        )
        by_compliance = defaultdict(list)
        for compliance_id, zone_id in pairs:
            by_compliance[compliance_id].append(zone_id)
        return [by_compliance[key] for key in keys]


class CurrencyByProductIdLoader(DataLoader):
    """The currency the merchant is typing a delta or a charge in.

    The admin asked this per rendered form (`label_money_field`,
    `compose/forms.py:88`), which is one query per page there and would be one
    query per ROW here. Cheapest listing first, because that is the listing the
    floor rule measures against; None when the product is in no channel, which
    the contract says is a null rather than a guess at the shop's default.
    """

    context_key = "wsm_currency_by_product"

    def batch_load(self, keys):
        from ....product.models import ProductChannelListing

        rows = (
            ProductChannelListing.objects.using(self.database_connection_name)
            .filter(product_id__in=keys)
            .order_by("product_id", "discounted_price_amount", "pk")
            .values_list("product_id", "currency")
        )
        currencies: dict[int, str] = {}
        for product_id, currency in rows:
            currencies.setdefault(product_id, currency)
        return [currencies.get(key) for key in keys]
