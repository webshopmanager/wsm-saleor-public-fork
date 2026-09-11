# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose over GraphQL: the questions, the charges, and the disclosures.

One package per domain, mirroring `saleor/graphql/giftcard/`. `schema.py`
exports the two mixins the fork's root `WsmQueries` / `WsmMutations` inherit;
nothing else in here is imported from outside the package except by tests.
"""
