# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's dataloaders.

OWNERSHIP NOTE: created by the compose unit (U2) because
`WsmDealerTierOptionPrice.dealerGroup` resolves a group from the CODE stored on
a compose row, and a naive resolver would be one query per delta on a screen
that renders one delta per buyer group per choice. Unit U4 owns this file; when
it lands its own, keep U4's and fold these three loaders into it.
"""

from collections import defaultdict

from django.db.models import Count

from ....graphql.core.dataloaders import DataLoader
from ...dealer import models


class DealerGroupByCodeLoader(DataLoader):
    """Null for a code no group carries, which is what the contract promises.

    A tier row survives the deletion of the group it names, because the column
    is a CharField and not a foreign key; the screen shows the stored code with
    no group beside it rather than a row that cannot be rendered.
    """

    context_key = "wsm_dealer_group_by_code"

    def batch_load(self, keys):
        groups = {
            group.code: group
            for group in models.DealerGroup.objects.using(
                self.database_connection_name
            ).filter(code__in=keys)
        }
        return [groups.get(key) for key in keys]


class TierPriceCountByDealerGroupIdLoader(DataLoader):
    context_key = "wsm_tier_price_count_by_dealer_group"

    def batch_load(self, keys):
        counts: dict[int, int] = defaultdict(int)
        rows = (
            models.TierPrice.objects.using(self.database_connection_name)
            .filter(group_id__in=keys)
            .values_list("group_id")
            .annotate(total=Count("pk"))
        )
        for group_id, total in rows:
            counts[group_id] = total
        return [counts[key] for key in keys]


class CustomerCountByDealerGroupIdLoader(DataLoader):
    context_key = "wsm_customer_count_by_dealer_group"

    def batch_load(self, keys):
        counts: dict[int, int] = defaultdict(int)
        rows = (
            models.DealerCustomer.objects.using(self.database_connection_name)
            .filter(group_id__in=keys)
            .values_list("group_id")
            .annotate(total=Count("pk"))
        )
        for group_id, total in rows:
            counts[group_id] = total
        return [counts[key] for key in keys]
