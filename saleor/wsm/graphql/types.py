# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What every WSM GraphQL type and mutation shares, and nothing else.

Domain types live under their own package (`dealer/types.py` and its siblings),
because a type belongs with the model it wraps. What does NOT belong to any one
domain is the documentation category: Saleor groups its schema with an `@doc`
directive, and every field this fork adds should land in the same group whatever
domain it came from. One string, one owner, named once.
"""

# Stock's categories are in `saleor/graphql/core/doc_category.py`, a core file.
# Ours lives here instead, so adding a domain never edits one.
DOC_CATEGORY_WSM = "WSM"
