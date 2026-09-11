# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two container aggregates, batched, because both are LIST fields.

`memberCount` and `currencyCode` have no column behind them: each is a question
about other tables that the Dashboard list screens ask once per row. Resolved
inline they are 1+N, invisible on the detail screen the resolver was written
for and 100 extra queries on the page the merchant actually lives on. Keyed the
way the row already knows its key (a collection id, a kit id), so nothing has to
be looked up in order to do the lookup.
"""

from collections import defaultdict

from ....graphql.core.dataloaders import DataLoader


class MemberCountByCollectionIdLoader(DataLoader):
    """Published members per series collection: the publish rule's own count."""

    context_key = "wsm_series_member_count_by_collection"

    def batch_load(self, keys):
        from ....product.models import Product

        pairs = (
            Product.objects.using(self.database_connection_name)
            .filter(collections__id__in=keys, channel_listings__is_published=True)
            .values_list("collections__id", "pk")
            .distinct()
        )
        counts: dict[int, int] = defaultdict(int)
        for collection_id, _product_id in pairs:
            counts[collection_id] += 1
        return [counts[key] for key in keys]


class CurrencyCodeByKitIdLoader(DataLoader):
    """What a kit is priced in, read off its members' own listings.

    A kit carries no currency column because no WSM column does. The shop
    channel is the fallback while a kit is still empty, and it is ONE query for
    the whole page rather than one per empty kit.
    """

    context_key = "wsm_currency_code_by_kit"

    def batch_load(self, keys):
        from ....channel.models import Channel
        from ....product.models import ProductVariantChannelListing

        rows = (
            ProductVariantChannelListing.objects.using(self.database_connection_name)
            .filter(variant__wsm_kit_memberships__kit_id__in=keys)
            .order_by("pk")
            .values_list("variant__wsm_kit_memberships__kit_id", "currency")
        )
        currencies: dict[int, str] = {}
        for kit_id, currency in rows:
            currencies.setdefault(kit_id, currency)

        fallback = None
        if any(key not in currencies for key in keys):
            fallback = (
                Channel.objects.using(self.database_connection_name)
                .values_list("currency_code", flat=True)
                .first()
            )
        return [currencies.get(key) or fallback for key in keys]
