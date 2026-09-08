# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md ("Monkey patches", MP2).
"""Requirement 2.4, order-level half: an ENTIRE_ORDER discount misses dealer lines.

MP1 (no_stacking.py) covers LINE-level discounts. An ENTIRE_ORDER voucher and an
order promotion are not line-level discounts in Saleor: each is a single amount
computed from the whole subtotal and then spread across every line in proportion
to that line's share of it. So with MP1 alone a dealer line still loses its share.

The stock levers were looked for first, in the 3.23.31 source, and there is none:

- The two spread functions take a plain list of lines and divide by
  ``share = line_total / subtotal``. They read no flag, skip no line and have no
  hook. ``is_gift``, the one line-shaped exclusion in stock discount code, is a
  voucher-side filter in ``get_discounted_lines`` and never reaches either of them.
- ``price_override`` is not a special case anywhere on this path. It sets the unit
  price and is then treated like any other price:
  ``CheckoutLineInfo.variant_discounted_price`` returns it and the spread divides
  it up with the rest.
- Excluding the line upstream, by keeping it out of the ``lines`` list a caller
  passes down, is not available either: that same list is the subtotal, the tax
  base and the order-line source. A line dropped from it is a line nobody bills.

So this is MP2, and like MP1 nothing is reimplemented. Five functions are wrapped;
each wrapper calls the original with a smaller set of lines and puts the dealer
lines back at the price they already had. Two are the discount AMOUNT (the base the
percentage is taken of), two are the SPREAD (the base it is divided over), and one
is shared by the checkout and the order for order promotions. Amount and spread
have to move together: fixing only the spread would take a discount sized on the
dealer line's money and hand all of it to the retail lines, which is worse than
stock.

Cost when it does nothing: zero. Every wrapper's first act is a dict-key test on
lines already in memory. With no dealer line among them the original runs on the
original arguments, and no query, no settings read and no Money arithmetic is
added. The toggle is consulted only once a dealer line is present, and it is the
same per-process cached read MP1 uses.

Upstream change that deletes this file: an exclusion honoured by the order-level
discount base, for example a ``discountable`` predicate on the line consulted by
``base_checkout_subtotal`` and by both propagate functions, the way ``is_gift``
is already consulted on the voucher side.
"""

from __future__ import annotations

from functools import wraps

from .no_stacking import install_guard, is_dealer_line, stacking_enabled

_installed = False


def _split_line_infos(lines_info):
    """(dealer, retail) over LineInfo objects. One pass, no query."""
    dealer, retail = [], []
    for info in lines_info:
        (dealer if is_dealer_line(info.line) else retail).append(info)
    return dealer, retail


def _split_lines(lines):
    """(dealer, retail) over bare OrderLine objects. One pass, no query."""
    dealer, retail = [], []
    for line in lines:
        (dealer if is_dealer_line(line) else retail).append(line)
    return dealer, retail


def _order_lines_total(lines, currency):
    """Base total of the given order lines: line discounts in, order-level out."""
    from prices import Money

    from ...order.base_calculations import base_order_line_total

    total = Money(0, currency)
    for line in lines:
        total += base_order_line_total(line).price_with_discounts.net
    return total


# --- 1. the checkout-side AMOUNT ---------------------------------------------


def checkout_voucher_amount_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def get_voucher_discount_for_checkout(
        manager, voucher, checkout_info, lines, address
    ):
        from ...discount.utils.voucher import is_order_level_voucher

        dealer, retail = _split_line_infos(lines)
        if not dealer or not is_order_level_voucher(voucher) or stacking_enabled():
            return original(manager, voucher, checkout_info, lines, address)
        # Retail lines only, so the percentage is taken of the money the voucher
        # is allowed to touch. It also means the voucher's minimum spend and
        # minimum quantity are judged on those same lines, which is the same rule
        # read the other way round: a dealer line cannot help qualify a checkout
        # for a discount that dealer line will not receive.
        return original(manager, voucher, checkout_info, retail, address)

    return get_voucher_discount_for_checkout


# --- 2. the checkout-side SPREAD ---------------------------------------------


def checkout_spread_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def _propagate_checkout_discount_on_checkout_lines_prices(
        lines, total_discount, currency
    ):
        dealer, retail = _split_line_infos(lines)
        if not dealer or stacking_enabled():
            yield from original(lines, total_discount, currency)
            return

        from ...checkout.base_calculations import calculate_base_line_total_price

        yield from original(retail, total_discount, currency)
        for info in dealer:
            yield info.line, calculate_base_line_total_price(info)

    return _propagate_checkout_discount_on_checkout_lines_prices


# --- 3. the order-side AMOUNT ------------------------------------------------


def order_amount_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def propagate_order_discount_on_order_prices(order, lines):
        dealer, retail = _split_lines(lines)
        if not dealer or stacking_enabled():
            return original(order, lines)
        # The original resizes every OrderDiscount row from the subtotal it is
        # handed, so it has to be handed the retail subtotal. The dealer money
        # goes back afterwards: it belongs in the order total, not in the base.
        subtotal, shipping_price = original(order, retail)
        return subtotal + _order_lines_total(dealer, order.currency), shipping_price

    return propagate_order_discount_on_order_prices


# --- 4. the order-side SPREAD ------------------------------------------------


def order_spread_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def propagate_order_discount_on_order_lines_prices(
        lines, base_subtotal, subtotal_discount
    ):
        dealer, retail = _split_lines(lines)
        if not dealer or stacking_enabled():
            yield from original(lines, base_subtotal, subtotal_discount)
            return

        from ...order.base_calculations import base_order_line_total

        dealer_total = _order_lines_total(dealer, base_subtotal.currency)
        yield from original(retail, base_subtotal - dealer_total, subtotal_discount)
        for line in dealer:
            yield line, base_order_line_total(line).price_with_discounts.net

    return propagate_order_discount_on_order_lines_prices


# --- 5. the order-promotion AMOUNT, checkout and order both ------------------


def order_promotion_amount_guard(original):
    """The wrapper, given the function it wraps, so a test can build its own."""

    @wraps(original)
    def create_discount_objects_for_order_promotions(
        order_or_checkout, lines_info, subtotal, channel, country, **kwargs
    ):
        dealer, _retail = _split_line_infos(lines_info)
        if not dealer or stacking_enabled():
            return original(
                order_or_checkout, lines_info, subtotal, channel, country, **kwargs
            )

        from prices import Money

        # variant_discounted_price is the one per-unit price that both
        # CheckoutLineInfo and EditableOrderLineInfo expose; on a dealer line it
        # is the tier. Taking it out of the base also takes it out of the rule's
        # own threshold test, which is the same ruling: dealer money does not buy
        # a discount it cannot receive.
        dealer_total = Money(0, subtotal.currency)
        for info in dealer:
            dealer_total += info.variant_discounted_price * info.line.quantity
        base = max(subtotal - dealer_total, Money(0, subtotal.currency))
        return original(order_or_checkout, lines_info, base, channel, country, **kwargs)

    return create_discount_objects_for_order_promotions


# --- install -----------------------------------------------------------------


def install() -> None:
    """Called once from DealerConfig.ready(), after MP1.

    Every one of the five is rebound at every module that holds it, discovered
    against the sites pinned in `saleor/wsm/patches.py`. Patching the definer
    alone was enough for none of them: `saleor.plugins.manager` and the two
    discount helpers each imported one by name, and an upstream bump that adds a
    sixth site now fails at boot instead of quietly discounting a dealer line.
    """
    global _installed
    if _installed:
        return
    _installed = True

    install_guard(
        "saleor.checkout.utils.get_voucher_discount_for_checkout",
        checkout_voucher_amount_guard,
    )
    install_guard(
        "saleor.checkout.base_calculations."
        "_propagate_checkout_discount_on_checkout_lines_prices",
        checkout_spread_guard,
    )
    install_guard(
        "saleor.order.base_calculations.propagate_order_discount_on_order_prices",
        order_amount_guard,
    )
    install_guard(
        "saleor.order.base_calculations."
        "propagate_order_discount_on_order_lines_prices",
        order_spread_guard,
    )
    install_guard(
        "saleor.discount.utils.promotion."
        "create_discount_objects_for_order_promotions",
        order_promotion_amount_guard,
    )
