"""WSM-FORK: fork-only Django app.

Everything WSM adds to Saleor's data layer lives here rather than inside an
upstream app, so that `saleor/product/` stays byte-identical to upstream and
neither its `models.py` nor its migration graph is part of the fork's deviation
set. See `models.py` for what this holds today.
"""
