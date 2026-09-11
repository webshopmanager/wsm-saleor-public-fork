# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""An account on hold places no orders, by any payment method.

**Why this seam, again.** `ComposeCompliancePlugin` picked
`preprocess_order_creation` for shipping restrictions and wrote down why
(`saleor/wsm/compose/plugin.py`): it is the only plugin-manager hook that fires
at completion, core calls it on all three completion paths, a plugin there can
REFUSE an order, and it costs one line in `saleor/settings.py` and NO monkey
patch. Every one of those reasons holds here, so this is the same shape rather
than a second idea.

Hold has to be enforced HERE and not in the terms mutation, because hold is not
a terms rule. On 5.0, 64 tenants and $308M TTM block checkout on account status,
card orders included. A held dealer who simply picks the card option has to be
refused by the same rule, and the only way to say that once is to say it on the
path every checkout takes.

**Cost.** Nothing on any browse, PDP, cart or shipping request: this runs once
per order creation. Inside it, nothing at all for a guest checkout (no user id,
no query) and one indexed read on a OneToOne for a signed-in one.
"""

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError

from ...plugins.base_plugin import BasePlugin
from .terms import HOLD_MESSAGE, is_on_hold

if TYPE_CHECKING:
    from ...checkout.fetch import CheckoutInfo, CheckoutLineInfo


class DealerAccountHoldPlugin(BasePlugin):
    """Refuse an order for a dealer account the merchant has put on hold."""

    PLUGIN_ID = "wsm.dealer.account_hold"
    PLUGIN_NAME = "WSM dealer account hold"
    DEFAULT_ACTIVE = True
    CONFIGURATION_PER_API = False

    def preprocess_order_creation(
        self,
        checkout_info: "CheckoutInfo",
        lines: "list[CheckoutLineInfo] | None",
        previous_value: Any,
    ):
        user_id = checkout_info.checkout.user_id
        if not user_id:
            return previous_value
        if not is_on_hold(user_id):
            return previous_value
        raise ValidationError(HOLD_MESSAGE)
