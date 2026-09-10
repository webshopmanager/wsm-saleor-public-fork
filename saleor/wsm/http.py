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

import base64
import binascii
import hmac
import uuid
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


def refuses_malformed_ids(as_json):
    """Turn a `MalformedId` raised inside a view into a 400 in the app's own shape.

    Compose and containers answer `{"violations": [...]}`, dealer answers
    `{"error": code}`, and the storefront route parses one or the other, so the
    shape is the argument and the refusal itself is shared. Before this, three
    view modules decoded a global id and handed whatever was inside straight to
    the ORM: `base64("Checkout:junk")` reached `uuid.UUID()` and a bare `garbage`
    reached `int()`, both as a 500 on a key-gated money endpoint. A 500 is a
    stack trace and a retry; this is an answer.
    """

    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except MalformedId as bad:
                return JsonResponse(as_json(str(bad)), status=400)

        return guarded

    return decorate


class MalformedId(ValueError):
    """An id that arrived and is not a well-formed key of the type asked for."""


def _is_uuid(pk: str) -> bool:
    try:
        uuid.UUID(pk)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


# `Checkout.pk` and `CheckoutLine.pk` are UUIDs; every other key the fork takes
# off the wire is an AutoField. The shape is the model's, so it is named at the
# call site and checked here.
PK_SHAPES = {"int": lambda pk: pk.isdigit(), "uuid": _is_uuid}


def global_pk(raw, expected: str, *, shape: str = "int", allow_raw: bool = False):
    """The primary key inside a Saleor global id, checked against the key's SHAPE.

    Returns None when nothing was sent, so a view keeps answering its own 404 for
    an id it was never given. Raises `MalformedId` when something WAS sent and is
    not a key of `expected`.

    That refusal is the point. Three view modules each decoded a global id and
    handed whatever was inside straight to the ORM, so `base64("Checkout:junk")`
    reached `uuid.UUID()` and a bare `garbage` reached `int()`: both came back as
    a 500 on a key-gated money endpoint, where the contract says 404 or 422. A
    500 is a stack trace in the log and a retry from the caller; a refusal is an
    answer.

    Padding is restored before decoding: a global id that lost its `=` in a URL
    is a routine thing to receive and refusing it would be a false 404.

    `allow_raw` keeps the dealer endpoints' documented habit of taking a bare
    primary key from the admin and from curl. The shape check still applies, so
    `17` is accepted where `garbage` is not.

    One function for what was `compose._from_gid`, its byte-identical
    `containers._from_gid` copy, and `dealer._pk`: the pk-shape check exists once
    rather than in three places that would each have to be remembered.
    """
    if raw is None or raw == "":
        return None
    text = str(raw)
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        decoded = ""
    type_name, sep, pk = decoded.partition(":")
    if sep and type_name == expected and pk:
        candidate = pk
    elif allow_raw and not sep:
        candidate = text
    else:
        raise MalformedId(f"{expected.lower()} id is malformed")
    if not PK_SHAPES[shape](candidate):
        raise MalformedId(f"{expected.lower()} id is malformed")
    return candidate


def buyer_mismatch(checkout, customer_pk) -> bool:
    """True when the checkout names a user and the caller named a different one.

    The storefront resolves the buyer server-side from its own httpOnly session
    cookie and never sends a mismatched pair, so this refuses nothing it sends.
    It is the fork's SECOND piece of evidence about who is buying: a checkout
    that carries a user is independent of the body, and throwing that away meant
    one leaked storefront key bought any price into any signed-in cart. With it,
    the same leak reaches anonymous carts only.

    An anonymous checkout has no opinion, which is the storefront's normal shape:
    the key-gated add resolves the customer and never attaches them.
    """
    if checkout.user_id is None or customer_pk is None:
        return False
    return str(checkout.user_id) != str(customer_pk)
