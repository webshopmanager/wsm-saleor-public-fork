# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The error type every WSM mutation returns, on stock's pattern.

Stock splits this in three across three core files: a plain `Enum` of codes per
domain (`saleor/giftcard/error_codes.py`), a graphene enum built from it
(`saleor/graphql/core/enums.py`), and an `Error` subclass carrying that enum
(`saleor/graphql/core/types/common.py`). The fork owns no core file, so the
three live here, in that order, and one module is the whole pattern.

One code set for the whole layer, not one per domain. The codes below are the
ones `get_error_code_from_error` can actually produce from a Django
`ValidationError` raised by our models, plus the two graphene raises itself;
a domain that needs a code of its own adds it here, where every screen that has
to render an error can see the complete list.
"""

from enum import Enum

import graphene

from ...graphql.core.types.common import Error
from .types import DOC_CATEGORY_WSM, WsmDocCategory


class WsmErrorCode(Enum):
    # The stock codes `get_error_code_from_error` can produce from any Django
    # ValidationError, plus the two graphene raises itself.
    GRAPHQL_ERROR = "graphql_error"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    # Not one stock produces from a ValidationError: stock refuses a whole
    # mutation with a top-level `PermissionDenied`. This layer needs the FIELD
    # shape as well, for a mutation the caller IS allowed to run that carries
    # one input field they are not (`tierDeltas`, dealer money on a catalog
    # mutation), so the screen can refuse the column and keep the save.
    PERMISSION_DENIED = "permission_denied"
    REQUIRED = "required"
    UNIQUE = "unique"

    # Compose. Every member below is a rule enforced on a model in
    # `saleor/wsm/compose/`, and the string is the `code=` on that raise; the
    # constants live beside the rule in `compose/models.py` so the two cannot
    # drift, and `test_every_compose_error_code_is_declared` says so.
    CONFIGURED_FLOOR_BELOW_ZERO = "configured_floor_below_zero"
    DEALER_FLOOR_BELOW_ZERO = "dealer_floor_below_zero"
    DUPLICATE_SKU_FRAGMENT = "duplicate_sku_fragment"
    UNKNOWN_DEALER_GROUP = "unknown_dealer_group"
    DEALER_DELTA_ABOVE_RETAIL = "dealer_delta_above_retail"
    DUPLICATE_TIER_GROUP = "duplicate_tier_group"
    FEE_AMOUNT_NEGATIVE = "fee_amount_negative"
    FEE_PERCENT_ABOVE_100 = "fee_percent_above_100"
    UNKNOWN_US_STATE_CODE = "unknown_us_state_code"
    # --- dealer: six rules, each with the line that enforces it. Two of them
    # are the same money column from two directions, because a tier amount is
    # what gets CHARGED: the one-cent floor
    # (CheckConstraint wsm_dealer_tier_amount_at_least_a_cent,
    # dealer/models.py:185), and the two decimal places a merchant is allowed
    # to type into a three-place column (dealer/admin.py:106).
    DUPLICATE_TIER_BREAK = "duplicate_tier_break"
    TIER_AMOUNT_BELOW_ONE_CENT = "tier_amount_below_one_cent"
    TIER_AMOUNT_TOO_MANY_DECIMALS = "tier_amount_too_many_decimals"
    DUPLICATE_GROUP_CODE = "duplicate_group_code"
    # The gated catalogue's one refusal, raised in `dealer/gate.py` as
    # `GATED_CODE` and returned by every WSM surface that can be handed a
    # product the requester may not buy. The STOCK checkout mutations cannot
    # carry it: `checkoutLinesAdd` returns `CheckoutError`, whose code is a
    # different enum in a core file this fork does not edit, so that surface
    # gets the same sentence under stock's own `product_unavailable`. See
    # `gate.stock_refusal`.
    CATALOGUE_GATED = "catalogue_gated"
    CUSTOMER_ALREADY_ASSIGNED = "customer_already_assigned"
    GROUP_IN_USE = "group_in_use"
    # A paste is capped on stock's own bulk-create shape (`MAX_ORDERS`,
    # `saleor/graphql/order/bulk_mutations/order_bulk_create.py:86`), and
    # this is stock's name for that refusal (`saleor/order/error_codes.py:86`).
    BULK_LIMIT = "bulk_limit"


# `from_enum` names the GraphQL type after the PYTHON class, so the type in the
# schema is `WsmErrorCode` whatever this variable is called.
WsmErrorCodeEnum = graphene.Enum.from_enum(WsmErrorCode)
WsmErrorCodeEnum.doc_category = DOC_CATEGORY_WSM


class WsmError(WsmDocCategory, Error):
    code = WsmErrorCodeEnum(description="The error code.", required=True)

    class Meta:
        description = "Represents an error in a WSM mutation."


class WsmMutationMeta(WsmDocCategory):
    """Every WSM mutation returns `WsmError`, so no `Meta` has to say so.

    The same argument as `WsmDocCategory` one rung up. One error type for the
    whole layer (see the module docstring) restated in twenty-four `Meta`
    blocks is twenty-four places for the twenty-fifth to fall back to stock's
    own error type, which carries a code enum no WSM screen knows how to read.
    """

    @classmethod
    def __init_subclass_with_meta__(cls, error_type_class=WsmError, **kwargs):
        super().__init_subclass_with_meta__(error_type_class=error_type_class, **kwargs)
