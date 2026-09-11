# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Who may see a price, who may buy, and what asking costs per request.

ds (diversifiedshaftssolutions.com) is the next store to go live on 6.0 and it
is a GATED catalogue: 2,731 of its 2,732 products carry 5.0's `login_required`
with a public price of 0. The public may browse and search; only a signed-in
dealer sees a price or a buy button. Its five customer groups are VISIBILITY,
not pricing: the money comes from two price books (FOB and CIF) that are already
two `DealerGroup` rows with their own `TierPrice` ladders.

THE RULE, in one function (`is_gated`), because a second copy of it is a second
answer. Three inputs feed it and all three live in this app:

- `DealerSettings.catalogue_gated`: the whole store. Off by default.
- `DealerProductGate`: one row per product that says otherwise, and optionally
  names the groups allowed to see it. ds's one public product is a row with
  `login_required` false against a gated store; a trade tier that may see a part
  no other tier may is a row with groups.
- `DealerCategoryGate`: the same two answers for a whole branch of the tree.
  5.0 gates categories directly (76 tenants, 127 rows) and scopes them by group
  (40 tenants, 210 rows); ds alone carries 15 visibility rows and 5 login gates
  that a product-only gate would have dropped on day one.

PRECEDENCE is most-specific-wins, which is the only reading a merchant can
predict: a product's own row beats any category, the NEAREST gated ancestor
category beats a further one, and the store switch is what is left.

DEFAULT DENY, and the default is DENY only where the merchant has not spoken. A
missing gate on a gated store is gated. A missing gate on an ungated store is
not: an unconfigured store is a normal store, and a filter nobody configured must
not start hiding a merchant's prices. Everywhere the merchant HAS spoken, their
answer wins, including "this one is public".

A BUYER CAN BE IN MORE THAN ONE GROUP, and access is the UNION (Dana,
2026-09-11). 5.0's `customer_group_link` is many-to-many on 28 tenants and 2,533
customers, and on ds 156 customers are in the link table against 96 on the legacy
single-group column. PRICE stays one group, because "the dealer price" has to be
unambiguous on every line (`DealerCustomer.group`, the priority winner);
VISIBILITY is every group they are in (`DealerCustomer.access_groups`). So a
dealer in Jobber and WD Pallet sees the parts either group may see.

WHY THIS IS NOT A SALEOR CHANNEL. Channels were the obvious rung to reach for,
and they do not fit: `available_for_purchase_at` and `visible_in_listings` are
per channel and per product, so they say "nobody may buy this" or "nobody may
see this", never "this buyer may and that one may not". A channel per trade tier
would also be a whole config object per tier (Dana, 2026-09-01: channels are not
pricing tiers, and never model a tier or a gate as a channel). Stock carries
nothing that expresses a per-REQUESTER price gate, so the tables are ours; the
price surfaces stay stock's, answered with the null they are already declared to
allow.

COST, which is the reason this file is shaped the way it is. A gated catalogue
is gated on every browse, so the gate is on the hottest path in the app and it
must not be a query per product:

- The store switch is one indexed read of a one-row table, once per request.
- The buyer's groups are ONE query, once per request, and none at all for a
  shopper who is not signed in.
- The per-product rows are TWO queries for a whole page, batched through a
  dataloader: one for the gate rows, one for their group links, and the second
  only when the first found something.
- The category tree costs THREE more, and only on a store that has gated a
  category at all: the gate rows, their group links, and one pass over the
  ancestry ranges. MPTT gives containment as an integer comparison, so no
  recursive walk and nothing per product.

So a 100-product listing page costs at most seven queries to answer "gated?" for
all of it, a store that gates only products costs four, and a store that has
never turned any of this on costs two.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import NamedTuple

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError

from .models import (
    DealerCategoryGate,
    DealerCustomer,
    DealerProductGate,
    DealerSettings,
)

# The one string this refusal is named by, in the error code enum
# (`WsmErrorCode.CATALOGUE_GATED`) and on every raise below. One constant
# because a code a screen matches on is an API, not a label.
GATED_CODE = "catalogue_gated"

# What the shopper reads. Written for the shopper standing in a dealer-only
# store, not for the developer: it says what to DO, not what went wrong.
GATED_MESSAGE = "Sign in with a dealer account to buy this product."


class Gate(NamedTuple):
    """One rule's answer, as the two things that decide it."""

    login_required: bool
    # Empty means "any dealer group". Non-empty NAMES the groups that may see
    # it, which is what "trade tiers are visibility only" buys ds.
    group_codes: frozenset[str]


def is_gated(gate: Gate | None, *, site_gated: bool, buyer_groups) -> bool:
    """May this buyer NOT see this thing's price or buy it? Default deny.

    Every way through is named here, once. `buyer_groups` is the set of codes
    this buyer is in, empty for a shopper who is not a dealer (signed in or not:
    an account in no group buys at retail and is not a dealer).
    """
    if gate is not None:
        # The merchant spoke about this product or its category. Their answer
        # wins both ways, which is what lets ds publish its one public part on
        # a gated store.
        if not gate.login_required:
            return False
        if not buyer_groups:
            return True
        # No named groups means any dealer group. Named groups means these, and
        # ANY overlap is enough: access is the union of the buyer's groups.
        return bool(gate.group_codes) and not (gate.group_codes & buyer_groups)
    if not site_gated:
        return False
    return not buyer_groups


def _group_codes_by_gate(model, gate_pks, *, db) -> dict[int, set[str]]:
    """The group links of these gate rows, off the through table, in ONE query.

    Values rather than instances and the through table rather than
    `prefetch_related`, because this runs on a product listing and nothing here
    needs a model: the answer is a set of strings.
    """
    codes: dict[int, set[str]] = defaultdict(set)
    column = f"{model._meta.model_name}_id"
    links = (
        model.groups.through.objects.using(db)
        .filter(**{f"{column}__in": gate_pks})
        .values_list(column, "dealergroup__code")
    )
    for gate_pk, code in links:
        codes[gate_pk].add(code)
    return codes


def gates_for_products(
    product_ids: Iterable[int], *, database_connection_name=None
) -> dict[int, Gate]:
    """The per-product rows for these products, as at most two queries.

    The second query is skipped entirely when the first found no rows, which is
    every store that has not gated an individual product.
    """
    ids = list(product_ids)
    if not ids:
        return {}
    db = database_connection_name or django_settings.DATABASE_CONNECTION_REPLICA_NAME
    rows = list(
        DealerProductGate.objects.using(db)
        .filter(product_id__in=ids)
        .values_list("pk", "product_id", "login_required")
    )
    if not rows:
        return {}
    codes = _group_codes_by_gate(DealerProductGate, [row[0] for row in rows], db=db)
    return {
        product_id: Gate(login_required, frozenset(codes.get(gate_pk, ())))
        for gate_pk, product_id, login_required in rows
    }


def category_gates_for_products(
    product_ids: Iterable[int], *, database_connection_name=None
) -> dict[int, Gate]:
    """The NEAREST gated ancestor category of each product, as a gate.

    Three queries on a store that has gated a category and ONE on a store that
    has not, whatever the page size. MPTT is why: a category is inside a gated
    branch when its `tree_id` matches and its `lft` falls between that branch's
    `lft` and `rght`, so containment is an integer comparison in python rather
    than a recursive walk or a query per product. Deepest `level` wins, which is
    what "most specific" means on a tree.

    ponytail: the ceiling is that EVERY gate row is read, not just the ones
    these products could sit under. Live data is 15 rows on ds and 61 on the
    widest tenant in the fleet sweep, so the read is smaller than the WHERE
    clause that would narrow it. The upgrade, if a tenant ever gates hundreds of
    categories, is to filter by the `tree_id` set of the products in hand.
    """
    ids = list(product_ids)
    if not ids:
        return {}
    db = database_connection_name or django_settings.DATABASE_CONNECTION_REPLICA_NAME
    rows = list(
        DealerCategoryGate.objects.using(db).values_list(
            "pk", "category_id", "login_required"
        )
    )
    if not rows:
        return {}

    from ...product.models import Category, Product

    codes = _group_codes_by_gate(DealerCategoryGate, [row[0] for row in rows], db=db)
    branches = {
        pk: (tree_id, lft, rght, level)
        for pk, tree_id, lft, rght, level in Category.objects.using(db)
        .filter(pk__in=[row[1] for row in rows])
        .values_list("pk", "tree_id", "lft", "rght", "level")
    }
    placed = {
        pk: (tree_id, lft)
        for pk, tree_id, lft in Category.objects.using(db)
        .filter(products__id__in=ids)
        .distinct()
        .values_list("pk", "tree_id", "lft")
    }
    category_of = dict(
        Product.objects.using(db).filter(pk__in=ids).values_list("pk", "category_id")
    )

    # category pk -> the gate of its deepest gated ancestor, resolved once per
    # distinct category rather than once per product.
    by_category: dict[int, Gate] = {}
    for category_pk, (tree_id, lft) in placed.items():
        best_level = -1
        winner = None
        for gate_pk, gate_category_pk, login_required in rows:
            branch = branches.get(gate_category_pk)
            if branch is None:
                continue
            branch_tree, branch_lft, branch_rght, level = branch
            if branch_tree != tree_id or not (branch_lft <= lft <= branch_rght):
                continue
            if level > best_level:
                best_level = level
                winner = Gate(login_required, frozenset(codes.get(gate_pk, ())))
        if winner is not None:
            by_category[category_pk] = winner

    return {
        product_id: by_category[category_pk]
        for product_id, category_pk in category_of.items()
        if category_pk in by_category
    }


def gated_products(
    product_ids: Iterable[int],
    *,
    buyer_groups,
    database_connection_name=None,
) -> dict[int, bool]:
    """`is_gated` for a whole page, in the query budget the module docstring sets."""
    ids = list(product_ids)
    site_gated = DealerSettings.catalogue_gated_enabled(
        database_connection_name=database_connection_name
    )
    gates = gates_for_products(ids, database_connection_name=database_connection_name)
    from_category = category_gates_for_products(
        ids, database_connection_name=database_connection_name
    )
    if not site_gated and not gates and not from_category:
        # The common store: nothing is gated, and nothing else is asked.
        return dict.fromkeys(ids, False)
    return {
        product_id: is_gated(
            # Most specific wins: the product's own row, then its nearest gated
            # ancestor category, then the store switch.
            gates.get(product_id) or from_category.get(product_id),
            site_gated=site_gated,
            buyer_groups=buyer_groups,
        )
        for product_id in ids
    }


def buyer_groups_for(user_pk, *, database_connection_name=None) -> frozenset[str]:
    """Every group code this buyer is in, price group included, in ONE query.

    The union, because a dealer's ACCESS is every group they belong to while
    their PRICE is the single `group` (`pricing.tier_group_for`). Costs nothing
    for a shopper who is not signed in: no id, no query.
    """
    if not user_pk:
        return frozenset()
    db = database_connection_name or django_settings.DATABASE_CONNECTION_REPLICA_NAME
    rows = (
        DealerCustomer.objects.using(db)
        .filter(user_id=user_pk)
        .values_list("group__code", "access_groups__code")
    )
    codes = set()
    for price_code, access_code in rows:
        if price_code:
            codes.add(price_code)
        if access_code:
            codes.add(access_code)
    return frozenset(codes)


def buyer_groups_for_request(context) -> frozenset[str]:
    """The groups this REQUEST buys and browses at, read once and remembered.

    Cached on the request rather than on a dataloader, because two different
    loaders and the checkout guard all ask the same question about the same
    request and the answer cannot change inside one.
    """
    cached = getattr(context, "_wsm_buyer_groups", None)
    if cached is not None:
        return cached
    user = getattr(context, "user", None)
    groups = frozenset()
    if user is not None and getattr(user, "is_authenticated", False):
        groups = buyer_groups_for(user.pk)
    context._wsm_buyer_groups = groups
    return groups


def requestor_may_see_prices(context) -> bool:
    """A staff token or app that manages products is never a gated shopper.

    The gate is about SHOPPERS. A merchant reading their own catalogue through
    the Dashboard has to see the prices they are editing, and an app with
    MANAGE_PRODUCTS is the merchant. `is_staff` is a column already on the
    fetched row, so a plain shopper is answered without the permission lookup
    that `has_perm` would otherwise make on the hottest path in the app.
    """
    from ...app.models import App
    from ...permission.enums import ProductPermissions

    requestor = getattr(context, "app", None) or getattr(context, "user", None)
    if requestor is None:
        return False
    if isinstance(requestor, App) or getattr(requestor, "is_staff", False):
        return bool(requestor.has_perm(ProductPermissions.MANAGE_PRODUCTS))
    return False


def refusal(message: str = GATED_MESSAGE) -> ValidationError:
    """The refusal every WSM-owned surface returns: `WsmErrorCode.CATALOGUE_GATED`."""
    return ValidationError(message, code=GATED_CODE)


def stock_refusal(message: str) -> ValidationError:
    """The same refusal on a STOCK mutation, under a code that enum can carry.

    `checkoutLinesAdd` and `checkoutCreate` return `CheckoutError`, whose `code`
    is `CheckoutErrorCode`: a different enum, declared in a core file this fork
    does not edit, and `required=True`. A value that enum does not know is not
    an unknown-code error, it is a GraphQL serialization failure that replaces
    the merchant's whole `errors` list with a top-level error, so the shopper
    would read nothing at all. `product_unavailable_for_purchase` is stock's own
    code for "you may not buy this", raised by
    `validate_variants_available_for_purchase` two lines away, and the SENTENCE
    the shopper reads is ours either way.

    `WsmErrorCode.CATALOGUE_GATED` is the code on every surface this fork owns.
    """
    from ...checkout.error_codes import CheckoutErrorCode

    return ValidationError(
        message, code=CheckoutErrorCode.PRODUCT_UNAVAILABLE_FOR_PURCHASE.value
    )


def blocked_products(product_ids, buyer_pk, *, database_connection_name=None) -> set:
    """Which of these products this buyer may not buy. The WRITER, by default.

    A price about to be stamped on a checkout line has to be decided from the
    same database the line is written to, which is the rule `dealer/pricing.py`
    already states for its two writing callers.

    The buyer's groups are read LAST and only when something might be gated, so
    a store that has gated nothing pays for this and never for the group query.
    """
    ids = set(product_ids)
    if not ids:
        return set()
    db = database_connection_name or django_settings.DATABASE_CONNECTION_DEFAULT_NAME
    site_gated = DealerSettings.catalogue_gated_enabled(database_connection_name=db)
    gates = gates_for_products(ids, database_connection_name=db)
    from_category = category_gates_for_products(ids, database_connection_name=db)
    if not site_gated and not gates and not from_category:
        return set()
    buyer_groups = buyer_groups_for(buyer_pk, database_connection_name=db)
    return {
        product_id
        for product_id in ids
        if is_gated(
            gates.get(product_id) or from_category.get(product_id),
            site_gated=site_gated,
            buyer_groups=buyer_groups,
        )
    }


def refuse_gated_lines(checkout, variants, lines_data) -> None:
    """MP7's body: refuse a cart write that ADDS a gated variant.

    Only lines that add quantity are weighed. A shopper whose cart already holds
    a line the merchant has since gated must still be able to take it out, and
    `checkoutLinesUpdate` removes by sending quantity 0 through this same
    function.
    """
    adding = {
        str(data.variant_id)
        for data in lines_data
        if data.variant_id and data.quantity > 0
    }
    wanted = [variant for variant in variants if str(variant.pk) in adding]
    if not wanted:
        return
    blocked = blocked_products(
        {variant.product_id for variant in wanted}, checkout.user_id
    )
    if not blocked:
        return
    skus = sorted(
        variant.sku or str(variant.pk)
        for variant in wanted
        if variant.product_id in blocked
    )
    raise stock_refusal(f"{GATED_MESSAGE} ({', '.join(skus)})")
