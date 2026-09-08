# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two storefront paths, byte-for-byte what wsm-storefront develop sends.

No trailing slashes: the storefront's URLs have none, and APPEND_SLASH only
fires on a miss, so these patterns must match exactly as written.
"""

from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        # The product GID arrives raw. It is base64 and may carry `=` padding,
        # percent-encoded or not; Django hands us the decoded path either way.
        r"^api/storefront/products/saleor/(?P<product_gid>[^/]+)/option-sets$",
        views.option_sets,
        name="wsm-compose-option-sets",
    ),
    re_path(
        r"^api/checkout/configured-line$",
        views.configured_line,
        name="wsm-compose-configured-line",
    ),
]
