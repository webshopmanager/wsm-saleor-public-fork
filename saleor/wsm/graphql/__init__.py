# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The fork's GraphQL layer: our own schema, composed from stock's.

Nothing here patches Saleor. `schema.py` SUBCLASSES stock's `Query` and
`Mutation`, builds a second federated schema with the same arguments, and
`saleor/wsm/urls.py` serves that one at `graphql/` before it hands the rest of
the URL space back to `saleor.urls`. The whole attachment is one settings line
(`ROOT_URLCONF`), which is rung 1 of Bill's integration ladder, not rung 5.
"""
