# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every URL the fork serves, behind the single ``include`` in saleor/urls.py.

U3 (dealer) and U4 (containers) add themselves here, not to core's urls.py, so
the core touch stays at one line for the life of the branch.
"""

import os

from django.conf import settings
from django.urls import include, re_path
from django.views.static import serve as static_serve

from .compose.admin import site as compose_admin

urlpatterns = [
    re_path(r"^wsm/compose/", include("saleor.wsm.compose.urls")),
    re_path(r"^wsm/dealer_pricing/", include("saleor.wsm.dealer.urls")),
    re_path(r"^wsm/containers/", include("saleor.wsm.containers.urls")),
    # The merchant UI (bake-off design section 6). Its own AdminSite, not
    # `admin.site`: see saleor/wsm/compose/admin.py for why.
    re_path(r"^admin/", compose_admin.urls),
]


def static_routes():
    """Django serving /static/ itself, and only where nothing else will.

    Saleor mounts /static/ only under DEBUG, a bake-off box runs DEBUG off with
    no web server in front of uvicorn and no whitenoise, so without this the
    merchant screens render unstyled, which is a false negative on a screenshot
    review. The target deployment is ECS behind a CDN, where Django serving
    static files is a production defect, so the default is OFF and the one box
    that needs it says so: WSM_SERVE_STATIC=true.

    ponytail: the real answer at re-home is `collectstatic` plus the CDN prefix
    that already serves Saleor's media, and then this function is deleted.
    """
    if os.environ.get("WSM_SERVE_STATIC", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return []
    return [
        re_path(
            r"^static/(?P<path>.*)$",
            static_serve,
            {"document_root": settings.STATIC_ROOT},
            name="wsm-static",
        )
    ]


urlpatterns += static_routes()
