# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The kit endpoint. No trailing slash: APPEND_SLASH only fires on a miss."""

from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r"^api/checkout/kit-line$",
        views.kit_line,
        name="wsm-containers-kit-line",
    ),
]
