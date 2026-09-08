"""WSM-FORK: fork-owned package, and a Django app in its own right.

Two things live here. The sub-packages (`compose`, `dealer`, `containers`)
are each their own Django app with their own `wsm_`-prefixed tables. This
package itself is also an app (label `wsm`), holding what the fork adds to
Saleor's data layer without owning a table: today that is the read-only
`ProductSecondaryCategory` model in `models.py`, whose table PartsLogic
creates and populates, and the `Category.resolve_products` swap that unions
secondary membership into a category listing.

Keeping all of it here is what lets `saleor/product/` and `saleor/graphql/`
stay byte-identical to upstream. See docs/wsm/CORE-TOUCHES.md and
FORK-NOTES.md.
"""
