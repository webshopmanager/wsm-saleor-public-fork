# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer pricing's dataloader: a group, by the code another domain stored.

`WsmDealerTierOptionPrice.dealerGroup` resolves a group from the CODE on a
compose row, and a naive resolver would be one query per delta on a screen that
renders one delta per buyer group per choice. The two count loaders that were
here went with the merge: the dealer type answers `tierPriceCount` and
`customerCount` off the annotations its own list resolver already carries.
"""

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
