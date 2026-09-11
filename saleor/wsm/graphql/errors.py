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
    # Stock codes, produced by graphene and by Django's own validators.
    DUPLICATED_INPUT_ITEM = "duplicated_input_item"
    GRAPHQL_ERROR = "graphql_error"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    REQUIRED = "required"
    UNIQUE = "unique"

    # --- containers: every one of these is a rule with a line that enforces it.
    # The value is what the enforcing raise passes as its `code`, so the screen
    # reading the error and the model refusing the save never drift.
    AXIS_NOT_IN_AXES = "axis_not_in_axes"
    SERIES_NEEDS_TWO_MEMBERS = "series_needs_two_members"
    MEMBER_MISSING_PARTITIONING_ATTRIBUTE = "member_missing_partitioning_attribute"
    UNKNOWN_ATTRIBUTE_SLUG = "unknown_attribute_slug"
    DUPLICATE_KIT_MEMBER = "duplicate_kit_member"
    KIT_MEMBER_QUANTITY_BELOW_ONE = "kit_member_quantity_below_one"
    RULE_SUBJECT_NOT_IN_KIT = "rule_subject_not_in_kit"
    RULE_TARGET_NOT_IN_KIT = "rule_target_not_in_kit"


# `from_enum` names the GraphQL type after the PYTHON class, so the type in the
# schema is `WsmErrorCode` whatever this variable is called.
WsmErrorCodeEnum = graphene.Enum.from_enum(WsmErrorCode)
WsmErrorCodeEnum.doc_category = DOC_CATEGORY_WSM


class WsmError(Error):
    code = WsmErrorCodeEnum(description="The error code.", required=True)

    class Meta:
        description = "Represents an error in a WSM mutation."
        doc_category = DOC_CATEGORY_WSM
