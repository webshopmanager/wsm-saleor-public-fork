# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's dataloader: a group, by the code another domain stored.

`WsmDealerTierOptionPrice.dealerGroup` resolves a group from the CODE on a
compose row, and a naive resolver would be one query per delta on a screen that
renders one delta per buyer group per choice. The two count loaders that were
here went with the merge: the dealer type answers `tierPriceCount` and
`customerCount` off the annotations its own list resolver already carries.
"""

from ....graphql.core.dataloaders import DataLoader
from ...dealer import gate, models


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


class ProductGatedLoader(DataLoader):
    """May this requester NOT see these products' prices? One answer per page.

    The gated catalogue's hot path. A gated store is gated on every browse, so
    the question is asked about every product on every listing, and the whole
    point of the loader is that the answer costs the same for one product as for
    a hundred: see the cost paragraph in `saleor/wsm/dealer/gate.py`.

    DEFAULT DENY on a key the rule did not answer, which cannot happen today and
    is the right way for it to fail if it ever does: a missing verdict shows
    less, never more.
    """

    context_key = "wsm_product_gated"

    def batch_load(self, keys):
        # The merchant reading their own catalogue is not a gated shopper: see
        # `gate.requestor_may_see_prices`. Answered before any query.
        if gate.requestor_may_see_prices(self.context):
            return [False] * len(keys)
        verdicts = gate.gated_products(
            keys,
            buyer_groups=gate.buyer_groups_for_request(self.context),
            database_connection_name=self.database_connection_name,
        )
        return [verdicts.get(key, True) for key in keys]


class CategoryGateByCategoryIdLoader(DataLoader):
    """The merchant-facing gate ROW of a category, or None. Two queries."""

    context_key = "wsm_category_gate_by_category_id"

    def batch_load(self, keys):
        gates = {
            row.category_id: row
            for row in models.DealerCategoryGate.objects.using(
                self.database_connection_name
            )
            .filter(category_id__in=keys)
            .prefetch_related("groups")
        }
        return [gates.get(key) for key in keys]


class ProductGateByProductIdLoader(DataLoader):
    """The merchant-facing gate ROW, or None. Groups prefetched, two queries."""

    context_key = "wsm_product_gate_by_product_id"

    def batch_load(self, keys):
        gates = {
            row.product_id: row
            for row in models.DealerProductGate.objects.using(
                self.database_connection_name
            )
            .filter(product_id__in=keys)
            .prefetch_related("groups")
        }
        return [gates.get(key) for key in keys]
