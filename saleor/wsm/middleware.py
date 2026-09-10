# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Django's three admin middlewares, scoped to the admin's own URL prefix.

The merchant UI at ``/admin/`` (bake-off design section 6) hard-requires
session, auth and message state, so the fork put ``SessionMiddleware``,
``AuthenticationMiddleware`` and ``MessageMiddleware`` in ``MIDDLEWARE``. Listed
there they run on EVERY request the process serves, and one of them is not idle
outside the admin: ``AuthenticationMiddleware`` overwrites ``request.user`` with
a lazy ``AnonymousUser``, whatever was there before.

Saleor reads that attribute to decide who a request is
(``plugin_manager_promise`` in ``saleor/graphql/plugins/dataloaders.py`` does
``requestor = app or user``), and upstream's contract, pinned by
``saleor/core/tests/test_dataloaders.py``, is that a requestor is a ``User`` or
nothing. An always-on lazy ``AnonymousUser`` breaks both halves of it: an
anonymous API call stops resolving to ``None``, and a signed-in one stops
resolving to its own ``User``, so every plugin in the manager is handed the
wrong requestor. Merely asking whether that lazy object is truthy also loads it,
which is a ``django_session`` read charged to a request that never wanted a
session.

The admin is where the three are needed, so the admin is where they run. Every
other path, the GraphQL API included, is byte-for-byte upstream again.

Subclasses rather than wrapper functions, deliberately: ``django.contrib.admin``
checks ``MIDDLEWARE`` for an entry that is a SUBCLASS of each of these three
(admin.E408, E409, E410), so subclassing keeps the admin's own configuration
valid, and it keeps ``MiddlewareMixin``'s sync and async capability, which a
plain function would drop.
"""

from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware

# Where saleor/wsm/urls.py mounts the merchant admin. One string, so moving the
# mount moves the middleware with it.
ADMIN_PREFIX = "/admin/"


class AdminOnly:
    """Mixin: hand the request straight on unless it is bound for the admin.

    First in the MRO of each class below, so both hooks short-circuit before the
    real middleware sees anything. ``process_response`` is defined here even
    though ``AuthenticationMiddleware`` has none of its own, because
    ``MiddlewareMixin.__call__`` dispatches on the attribute merely existing;
    the ``getattr`` is what keeps that case honest.
    """

    def _is_admin(self, request) -> bool:
        return request.path.startswith(ADMIN_PREFIX)

    def process_request(self, request):
        if self._is_admin(request):
            return super().process_request(request)  # type: ignore[misc]
        return None

    def process_response(self, request, response):
        if not self._is_admin(request):
            return response
        parent = getattr(super(), "process_response", None)
        return parent(request, response) if parent is not None else response


class AdminSessionMiddleware(AdminOnly, SessionMiddleware):
    pass


class AdminAuthenticationMiddleware(AdminOnly, AuthenticationMiddleware):
    pass


class AdminMessageMiddleware(AdminOnly, MessageMiddleware):
    pass
