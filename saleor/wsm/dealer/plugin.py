# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The backstop under MP7: a gated product never becomes an ORDER.

MP7 refuses the cart write, which is where a shopper should read the sentence.
This is the layer under it, for every way a line can be in a cart that MP7 never
saw: a line added before the merchant gated the product, a checkout that changed
hands, a direct write by another app. `preprocess_order_creation` is the only
plugin-manager hook that fires at completion and it can REFUSE, which is exactly
what a gate wants (`saleor/wsm/compose/plugin.py` makes the same argument for
shipping restrictions, and core calls the hook on all three completion paths).

A native platform hook, so this costs ONE line in `saleor/settings.py` and no
monkey patch. It runs once per order creation and never on a browse, a PDP or a
cart page.
"""

from typing import TYPE_CHECKING, Any

from ...plugins.base_plugin import BasePlugin
from .gate import GATED_MESSAGE, blocked_products, stock_refusal

if TYPE_CHECKING:
    from ...checkout.fetch import CheckoutInfo, CheckoutLineInfo


class DealerGatePlugin(BasePlugin):
    """Refuse an order that contains a product this buyer may not buy."""

    PLUGIN_ID = "wsm.dealer.gate"
    PLUGIN_NAME = "WSM gated catalogue"
    DEFAULT_ACTIVE = True
    CONFIGURATION_PER_API = False

    def preprocess_order_creation(
        self,
        checkout_info: "CheckoutInfo",
        lines: "list[CheckoutLineInfo] | None",
        previous_value: Any,
    ):
        if lines is None:
            from ...checkout.fetch import fetch_checkout_lines

            lines, _unavailable = fetch_checkout_lines(checkout_info.checkout)
        product_ids = {line.product.id for line in lines}
        if not product_ids:
            return previous_value
        blocked = blocked_products(product_ids, checkout_info.checkout.user_id)
        if not blocked:
            return previous_value
        skus = sorted(
            line.variant.sku or str(line.variant.pk)
            for line in lines
            if line.product.id in blocked
        )
        raise stock_refusal(f"{GATED_MESSAGE} ({', '.join(skus)})")
