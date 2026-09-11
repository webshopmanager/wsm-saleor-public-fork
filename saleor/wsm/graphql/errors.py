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
from .types import DOC_CATEGORY_WSM


class WsmErrorCode(Enum):
    # The stock codes `get_error_code_from_error` can produce from any Django
    # ValidationError, plus the two graphene raises itself.
    GRAPHQL_ERROR = "graphql_error"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
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


# `from_enum` names the GraphQL type after the PYTHON class, so the type in the
# schema is `WsmErrorCode` whatever this variable is called.
WsmErrorCodeEnum = graphene.Enum.from_enum(WsmErrorCode)
WsmErrorCodeEnum.doc_category = DOC_CATEGORY_WSM


class WsmError(Error):
    code = WsmErrorCodeEnum(description="The error code.", required=True)

    class Meta:
        description = "Represents an error in a WSM mutation."
        doc_category = DOC_CATEGORY_WSM
