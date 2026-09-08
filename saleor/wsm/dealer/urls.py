# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.urls import path

from . import views

# No trailing slashes, and the `api/` segment is part of the contract: the live
# storefront posts to /wsm/dealer_pricing/api/... (src/lib/dealerPricing.ts), and
# it is spelled HERE, in the app's own file, exactly as the compose and container
# apps spell theirs. The mount in saleor/wsm/urls.py is then one `^wsm/<app>/`
# line per app with nothing app-specific hidden in it.
urlpatterns = [
    path("api/storefront/prices", views.storefront_prices, name="wsm-dealer-prices"),
    path("api/checkout/dealer-line", views.dealer_line, name="wsm-dealer-line"),
    path(
        "api/checkout/dealer-line/reprice",
        views.dealer_line_reprice,
        name="wsm-dealer-line-reprice",
    ),
]
