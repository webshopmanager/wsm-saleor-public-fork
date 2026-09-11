# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What every WSM GraphQL type and mutation shares, and nothing else.

Domain types live under their own package (`dealer/types.py` and its siblings),
because a type belongs with the model it wraps. What does NOT belong to any one
domain is the documentation category: Saleor groups its schema with an `@doc`
directive, and every field this fork adds should land in the same group whatever
domain it came from. One string, one owner, named once.

Named once is what `WsmDocCategory` is for. Stock spells `doc_category` in every
`Meta` because stock has one category PER DOMAIN, so there the value really is a
per-type decision. This layer has ONE value for the whole layer, and spelling it
on forty `Meta` blocks is forty chances for the forty-first to be missed: the
field lands in no documentation group and nothing goes red. The mixin supplies
it as the DEFAULT, so a type that ever needs another category still says so in
its own `Meta` and that wins.
"""

# Stock's categories are in `saleor/graphql/core/doc_category.py`, a core file.
# Ours lives here instead, so adding a domain never edits one.
DOC_CATEGORY_WSM = "WSM"


class WsmDocCategory:
    """The layer's doc category, as the default every WSM type inherits.

    Graphene hands a class's `Meta` to `__init_subclass_with_meta__` as keyword
    arguments, so a default declared here is exactly what the `Meta` line it
    replaces would have passed. First in the bases, so it runs ahead of the
    stock base it is mixed into.
    """

    @classmethod
    def __init_subclass_with_meta__(cls, doc_category=DOC_CATEGORY_WSM, **kwargs):
        super().__init_subclass_with_meta__(doc_category=doc_category, **kwargs)
