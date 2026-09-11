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

from django.urls import include, re_path
from django.views.decorators.csrf import csrf_exempt

from ..graphql.api import backend
from ..graphql.views import GraphQLView
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
    # Everything Saleor serves, unchanged and unedited.
    re_path(r"", include("saleor.urls")),
]
