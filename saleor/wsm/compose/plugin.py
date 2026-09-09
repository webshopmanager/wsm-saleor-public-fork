# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The one place a shipping restriction is enforced: order creation.

**Why this seam.** `preprocess_order_creation` is the only plugin-manager hook
that fires at completion, and MP3's own notes rejected it for repricing because
a plugin there can REFUSE an order but cannot correct one. Refusing is exactly
what a restriction wants: the shopper is not owed a correction, they are owed a
sentence saying the part cannot go to their state. Core calls it on all three
completion paths, so one hook covers every way a checkout becomes an order:

- `saleor/checkout/complete_checkout.py:754` (`_prepare_order_data`, the
  payment path),
- `saleor/checkout/complete_checkout.py:987` (the no-payment path),
- `saleor/checkout/checkout_cleaner.py:467` (`orderCreateFromCheckout`, which
  is the transaction flow the storefront actually uses).

That is a native platform hook, so this costs ONE line in
`saleor/settings.py::BUILTIN_PLUGINS` and NO monkey patch. Both call sites
catch only `TaxError`, so a `ValidationError` raised here travels to the
mutation and comes back as a checkout error the storefront already renders.

**Cost.** Nothing at all on any browse, PDP, cart or shipping-step request:
this method runs once per order creation. Inside it, one indexed read, and a
second only when a product in the order actually carries a compliance row.
"""

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError

from ...plugins.base_plugin import BasePlugin
from .restrictions import is_destination_serviced

if TYPE_CHECKING:
    from ...checkout.fetch import CheckoutInfo, CheckoutLineInfo


class ComposeCompliancePlugin(BasePlugin):
    """Refuse an order whose destination a product in it is not serviced for."""

    PLUGIN_ID = "wsm.compose.compliance"
    PLUGIN_NAME = "WSM shipping restrictions"
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

        # The address the goods go to. A checkout that requires no shipping has
        # none, and its billing address is where the merchant is sending
        # anything at all; on a checkout that does ship, the two are the same
        # unless the shopper said otherwise, in which case shipping wins.
        address = checkout_info.shipping_address or checkout_info.billing_address
        serviced, refusals = is_destination_serviced(product_ids, address)
        if serviced:
            return previous_value
        raise ValidationError(" ".join(r.message for r in refusals))
