# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The three closed value sets compose stores, as GraphQL enums.

Built FROM `compose.pricing`'s own tuples rather than re-spelled, so a prompt
type added to the pricing engine is a failing assertion here rather than a
choice the Dashboard silently cannot send. The names are the contract's
(`WsmOptionSetPromptType`, `WsmFeeBasis`, `WsmFeeScope`); the values are the
strings already in the database, which is why `PER_UNIT` is `"unit"`.
"""

import graphene

from ...compose import pricing
from ..types import DOC_CATEGORY_WSM

PROMPT_TYPE_NAMES = {
    pricing.CHOICE_ONE: "CHOICE_ONE",
    pricing.CHOICE_MANY: "CHOICE_MANY",
    "text": "TEXT",
    "date": "DATE",
    "datetime": "DATETIME",
    "image": "IMAGE",
}
FEE_BASIS_NAMES = {pricing.FIXED: "FIXED", pricing.PERCENT: "PERCENT"}
FEE_SCOPE_NAMES = {pricing.PER_UNIT: "PER_UNIT", pricing.PER_LINE: "PER_LINE"}


def _enum(type_name: str, values: tuple[str, ...], names: dict[str, str]):
    missing = [value for value in values if value not in names]
    if missing:
        raise RuntimeError(
            f"{type_name} has no GraphQL name for {missing}. A value the pricing "
            f"engine accepts and the schema cannot say is a value the Dashboard "
            f"cannot send."
        )
    enum = graphene.Enum(type_name, [(names[value], value) for value in values])
    enum.doc_category = DOC_CATEGORY_WSM
    return enum


WsmOptionSetPromptTypeEnum = _enum(
    "WsmOptionSetPromptType", tuple(pricing.PROMPT_TYPES), PROMPT_TYPE_NAMES
)
WsmFeeBasisEnum = _enum("WsmFeeBasis", tuple(pricing.FEE_BASES), FEE_BASIS_NAMES)
WsmFeeScopeEnum = _enum("WsmFeeScope", tuple(pricing.FEE_SCOPES), FEE_SCOPE_NAMES)
