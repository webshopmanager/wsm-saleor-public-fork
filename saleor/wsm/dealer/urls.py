# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.urls import path

from . import views

# No trailing slashes: the storefront posts to these paths exactly.
urlpatterns = [
    path("storefront/prices", views.storefront_prices, name="wsm-dealer-prices"),
    path("checkout/dealer-line", views.dealer_line, name="wsm-dealer-line"),
    path(
        "checkout/dealer-line/reprice",
        views.dealer_line_reprice,
        name="wsm-dealer-line-reprice",
    ),
]
