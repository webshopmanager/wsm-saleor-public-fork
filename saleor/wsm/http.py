# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Who is allowed to call the fork's write endpoints.

The trust model is the one wsm-storefront develop already speaks. The storefront
SERVER, never a browser, calls these endpoints: `src/lib/dealerPricing.ts` and
`src/lib/configuredLine.ts` run server-side, resolve the buyer from their own
session, and prove they are the storefront with a per-tenant secret sent as
`X-Dealer-Pricing-Key` (dealer) or `X-Compose-Key` (compose).

The forgeable thing was never a PRICE, which is why "nothing a caller could
forge" read true and was wrong: it is the BUYER. `customerId` arrives in the
body, so an unauthenticated caller could ask for any user's dealer ladder, put a
line in any checkout at that user's tier price, and read what a competitor pays.
One key per process is the right grain because one Saleor process serves one
tenant (design section 6), so the key IS the tenant.

Fails SAFE: an unset or empty `WSM_STOREFRONT_KEY` closes every gated endpoint
rather than opening it. A tenant that forgot to set the secret sells nothing
through these routes, which is loud; the alternative is selling at anyone's
dealer price, which is silent.
"""

from __future__ import annotations

import hmac
from functools import wraps

from django.conf import settings
from django.http import JsonResponse

# The two spellings the storefront already sends, one per app. Both name the
# same per-process secret, so either satisfies either endpoint: the header name
# is the caller's habit, not a second identity.
KEY_HEADERS = ("HTTP_X_DEALER_PRICING_KEY", "HTTP_X_COMPOSE_KEY")


def _bytes(value) -> bytes:
    """Bytes for `compare_digest`, which refuses a str carrying non-ASCII."""
    return str(value or "").encode("utf-8", "replace")


def storefront_key_required(view):
    """Refuse anything that cannot present the tenant's storefront key.

    Constant-time compare, so the 401 does not leak the key one byte at a time.
    """

    @wraps(view)
    def guarded(request, *args, **kwargs):
        expected = _bytes(getattr(settings, "WSM_STOREFRONT_KEY", ""))
        presented = next(
            (
                _bytes(request.META[header])
                for header in KEY_HEADERS
                if request.META.get(header)
            ),
            b"",
        )
        if not expected or not hmac.compare_digest(presented, expected):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return view(request, *args, **kwargs)

    return guarded
