# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The ROOT urlconf. Every URL the fork serves, then everything Saleor serves.

The order is the design. `graphql/` is answered by the schema composed in
`saleor/wsm/graphql/schema.py`, which is stock's `Query` and `Mutation`
subclassed with ours mixed in; `saleor.urls` is then included WHOLE and
unedited, so every core route resolves exactly as it did and core's own
`graphql/` line is simply never reached. That is what lets the fork add fields
to the API with no monkey patch and no edit to a file Saleor owns: one
`ROOT_URLCONF` line in settings, which `docs/wsm/CORE-TOUCHES.md` counts.

It used to be the other way round, with `saleor/urls.py` carrying an `include`
of this module. That line is gone: under this urlconf it would be a cycle, and
removing it takes `saleor/urls.py` back to upstream byte-for-byte.
"""

import os

from django.conf import settings
from django.urls import include, re_path
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve as static_serve

from ..graphql.api import backend
from ..graphql.views import GraphQLView
from .compose.admin import site as compose_admin
from .graphql.schema import schema as wsm_schema

urlpatterns = [
    # FIRST, and named `api` like core's, because `reverse("api")` is how every
    # client and every test finds the endpoint. Both patterns spell the same
    # path, so the name resolves to `/graphql/` either way; what the order
    # decides is which SCHEMA answers, and it has to be the composed one.
    re_path(
        r"^graphql/$",
        csrf_exempt(GraphQLView.as_view(backend=backend, schema=wsm_schema)),
        name="api",
    ),
    re_path(r"^wsm/compose/", include("saleor.wsm.compose.urls")),
    re_path(r"^wsm/dealer_pricing/", include("saleor.wsm.dealer.urls")),
    re_path(r"^wsm/containers/", include("saleor.wsm.containers.urls")),
    # The merchant UI (bake-off design section 6). Its own AdminSite, not
    # `admin.site`: see saleor/wsm/compose/admin.py for why.
    re_path(r"^admin/", compose_admin.urls),
    # Everything Saleor serves, unchanged and unedited.
    re_path(r"", include("saleor.urls")),
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
