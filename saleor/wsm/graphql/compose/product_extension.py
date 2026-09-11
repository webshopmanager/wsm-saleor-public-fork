# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Three fields on stock's `Product` type, so the product page hosts a Compose tab.

WHY THIS IS NOT A CORE EDIT, and what it costs.

The contract's section 7 extends `Product`, because a merchant opening a product
wants its questions, its charges and its disclosure in the SAME round trip the
page already makes. Stock has no extension point for that: a graphene 2
`ObjectType` collects its fields when the class body runs, and the only public
handle on them afterwards is `_meta.fields`, the ordered dict the type map reads
at schema-build time (`graphene/types/typemap.py`,
`construct_fields_for_type`). So the three fields are appended to that dict here,
from our own module, and `saleor/graphql/product/types/products.py` is not
touched.

ponytail: the ceiling is that this is a mutation of a core CLASS, invisible from
the file it changes, which is the same thing that makes a monkey patch
expensive. Two things keep it cheap. It is additive and idempotent, so it cannot
change the meaning of a field stock already has (`_assert_free` refuses to
overwrite one). And it is ordered: `saleor.graphql.api` is imported at the top
of this module, which BUILDS stock's own schema before anything is added, so the
schema served at `graphql/` is the only one that carries the fields. The upgrade
path, if Saleor ever grows a real extension hook, is to delete this module and
declare the fields there; nothing else in the package changes.

`saleor/wsm/tests/test_core_tables_untouched.py` proves no core FILE moved;
`test_stock_schema_does_not_carry_the_wsm_fields` proves the injection did not
leak into the schema stock builds for itself.
"""

from ....graphql import api  # noqa: F401  (imported for its ordering effect)
from ....graphql.core.fields import PermissionsField
from ....graphql.core.types import NonNullList
from ....graphql.product.types import Product
from ....permission.enums import ProductPermissions
from ..types import DOC_CATEGORY_WSM
from . import dataloaders as loaders
from .types import WsmFee, WsmOptionSet, WsmProductCompliance

WSM_PRODUCT_FIELDS = ("wsm_option_sets", "wsm_fees", "wsm_compliance")


def _resolve_option_sets(root, info, **_kwargs):
    return loaders.OptionSetsByProductIdLoader(info.context).load(root.node.id)


def _resolve_fees(root, info, **_kwargs):
    return loaders.FeesByProductIdLoader(info.context).load(root.node.id)


def _resolve_compliance(root, info, **_kwargs):
    return loaders.ComplianceByProductIdLoader(info.context).load(root.node.id)


def _assert_free(name):
    existing = Product._meta.fields.get(name)
    if existing is not None and getattr(existing, "_wsm_owned", False) is not True:
        raise RuntimeError(
            f"Product already declares {name!r}. This fork appends fields to the "
            f"stock type and never redefines one, so a collision is an upstream "
            f"bump to read, not a name to work around."
        )


def extend_product_type():
    """Append the three compose fields to stock's `Product`. Idempotent.

    The resolver is set on the CLASS and never passed as the field's
    `resolver=`, and that is not a style choice. `BaseField.get_resolver` ends
    with `self.resolver or parent_resolver`
    (`saleor/graphql/core/fields.py:36`), so a field carrying its own resolver
    DISCARDS the one `PermissionsField.get_resolver` just wrapped in the
    permission check, and the field answers any staff token. Left on the class,
    graphene finds it as `resolve_<name>` (`graphene/types/typemap.py:309`),
    hands it in as the parent resolver, and the gate survives.
    `test_the_tab_is_refused_to_a_staff_token_without_manage_products` is the
    test that caught it, red on the version that passed `resolver=`.
    """
    fields = {
        "wsm_option_sets": (
            PermissionsField(
                NonNullList(WsmOptionSet),
                required=True,
                description=(
                    "This product's option sets, ordered by sortOrder then pk."
                ),
                permissions=[ProductPermissions.MANAGE_PRODUCTS],
                doc_category=DOC_CATEGORY_WSM,
            ),
            _resolve_option_sets,
        ),
        "wsm_fees": (
            PermissionsField(
                NonNullList(WsmFee),
                required=True,
                description="The charges attached to this product.",
                permissions=[ProductPermissions.MANAGE_PRODUCTS],
                doc_category=DOC_CATEGORY_WSM,
            ),
            _resolve_fees,
        ),
        "wsm_compliance": (
            PermissionsField(
                WsmProductCompliance,
                description="Null when this product has no compliance row.",
                permissions=[ProductPermissions.MANAGE_PRODUCTS],
                doc_category=DOC_CATEGORY_WSM,
            ),
            _resolve_compliance,
        ),
    }
    return append_product_fields(fields)


def append_product_fields(fields):
    """Append `{name: (field, resolver)}` to stock's `Product`. Idempotent.

    The loop, named, because MP4 is not one domain's business any more: the
    dealer app appends the gated-catalogue fields to the same core type through
    this function (`saleor/wsm/graphql/dealer/product_extension.py`), and a
    second copy of this loop would be a second `_assert_free` and a second
    chance to forget the `_wsm_owned` marker the ledger discovers by.

    ponytail: the ceiling is that the ORDERING guarantee (stock's schema is
    built first, by the `saleor.graphql.api` import at the top of this module)
    lives in the compose package while a dealer module depends on it. The
    upgrade, the first time a third domain extends `Product`, is to lift this
    function and that import into `saleor/wsm/graphql/product_extension.py`,
    which is the layer that actually owns "fields on stock's Product"; nothing
    but the import lines would move.
    """
    for name, (field, resolver) in fields.items():
        _assert_free(name)
        field._wsm_owned = True
        Product._meta.fields[name] = field
        setattr(Product, f"resolve_{name}", staticmethod(resolver))
    return tuple(fields)


extend_product_type()


# Re-exported so a test can assert the extension without importing graphene's
# internals itself.
__all__ = ["WSM_PRODUCT_FIELDS", "append_product_fields", "extend_product_type"]
