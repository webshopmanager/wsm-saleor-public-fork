# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""A dealer's tax exemption, carried onto the checkout Saleor will tax.

5.0 spelled this as one checkbox on the dealer account, so this fork does too:
`DealerCustomer.tax_exempt`. What it MEANS is Saleor's own
`Checkout.tax_exemption`, the flag `saleor/checkout/calculations.py` reads to
skip tax and `saleor/checkout/complete_checkout.py` copies onto the order.
Nothing here calculates a tax, picks a rate or touches a tax class: it copies
one boolean from the account onto the cart, once, and stock Saleor does the
rest. That is the whole reason the exemption is wired to the native field
instead of a second flag of ours: an order placed on a tax-exempt cart is
exempt through the same code path a staff exemption takes, including the
`taxExemptionManage` audit surface and the `tax_exemption` field on the order.

Written on the key-gated storefront routes and NOWHERE else, which is what
makes it unforgeable. `customerId` arrives in a request body, so an exemption
that followed the body without the tenant key would be a tax-free order for
anyone who could name a dealer's user id (saleor/wsm/http.py). The stamped
group on a checkout line proves nothing here either: `saleor/wsm/reprice.py`
re-derives a price from that stamp on purpose, but any caller who can write a
line can write a group name onto it, so a stamp is never read as identity for
this flag. An anonymous checkout carrying a dealer stamp is taxed.

A retail shopper is left alone rather than written False. Somebody with no
dealer account is not this app's to answer for, and a merchant who exempted one
by hand through `taxExemptionManage` would otherwise have it silently undone by
that shopper's next add to cart. A DEALER's flag is written every time, True or
False, so an exemption a merchant revokes is off the cart on the next call
rather than at the next cart the shopper happens to start.
"""

from __future__ import annotations

from ...checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ...checkout.utils import invalidate_checkout
from ...plugins.manager import get_plugins_manager
from .models import DealerCustomer


def bind_tax_exemption(checkout, user_pk, *, database_connection_name) -> bool:
    """Copy this buyer's dealer exemption onto `checkout`. True when it moved.

    One query, from the id, on the buyer's OneToOne row: the callers already
    hold the user and none of them needs the `DealerCustomer` object itself.
    Costs nothing for a shopper who is not signed in.

    `database_connection_name` is required and keyword-only because every
    caller is a write path, and a flag read off a replica is a flag decided
    from a database the checkout was not written to.

    Expiring the prices is not optional and not the caller's to remember:
    `tax_exemption` is a pricing INPUT, so a cart that changed it without
    expiring keeps quoting the tax it no longer owes until something else
    happens to touch it. This is the same three lines `TaxExemptionManage`
    runs (`saleor/graphql/tax/mutations/tax_exemption_manage.py`), on the same
    fields, which is why a merchant flipping the flag in the dashboard and a
    dealer adding to cart leave the checkout in the same state.
    """
    if not user_pk:
        return False
    exempt = (
        DealerCustomer.objects.using(database_connection_name)
        .filter(user_id=user_pk)
        .values_list("tax_exempt", flat=True)
        .first()
    )
    if exempt is None or exempt == checkout.tax_exemption:
        return False

    checkout.tax_exemption = exempt
    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info.lines = lines
    invalidate_checkout(checkout_info, lines, manager, save=False)
    checkout.save(
        update_fields=[
            "tax_exemption",
            "price_expiration",
            "discount_expiration",
            "last_change",
        ]
    )
    return True
