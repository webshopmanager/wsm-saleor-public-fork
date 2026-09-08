# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every URL the fork serves, behind the single ``include`` in saleor/urls.py.

U3 (dealer) and U4 (containers) add themselves here, not to core's urls.py, so
the core touch stays at one line for the life of the branch.
"""

from django.conf import settings
from django.urls import include, re_path
from django.views.static import serve as static_serve

from .compose.admin import site as compose_admin

urlpatterns = [
    re_path(r"^wsm/compose/", include("saleor.wsm.compose.urls")),
    # The merchant UI (bake-off design section 6). Its own AdminSite, not
    # `admin.site`: see saleor/wsm/compose/admin.py for why.
    re_path(r"^admin/", compose_admin.urls),
    # The admin's own CSS and JS. Saleor mounts /static/ only under DEBUG, this
    # box runs DEBUG off, there is no web server in front of uvicorn and
    # whitenoise is not a Saleor dependency. Without this the merchant screens
    # render unstyled, which is a false negative on a screenshot review.
    # ponytail: correct for one box behind SSH; a real deployment puts static
    # behind the CDN that already serves Saleor's media.
    re_path(
        r"^static/(?P<path>.*)$",
        static_serve,
        {"document_root": settings.STATIC_ROOT},
        name="wsm-static",
    ),
]
