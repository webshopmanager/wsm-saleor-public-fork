# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Everything the fork mounts, under one prefix core includes once.

Core's urls.py gains a single line for /wsm/ however many apps land here, which
is the whole point: the next app is a line in THIS file, not a core touch.
"""

from django.urls import include, path

urlpatterns = [
    path("dealer_pricing/", include("saleor.wsm.dealer.urls")),
]
