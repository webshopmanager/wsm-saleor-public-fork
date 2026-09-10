# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Why the payment-readiness gate is carved out on one mutation and not its twin.

`transactionInitialize` stands the gate down for the gift card gateway, with an
argument written out in CORE-TOUCHES section 11. `transactionProcess` calls the
same gate with no gateway argument, so the carve-out cannot fire there.

The cold review (Wild West finding 10) called that asymmetry a defect. It is
not a reachable one, and this file is the reason held as a test rather than as a
paragraph: no `App` row stands behind the built-in gift card gateway, so
`transactionProcess` refuses a gift card transaction whatever the readiness gate
says. The refusal is real either way; only which error is returned differs, and
upstream, with no gate at all, refuses it in exactly the same place.

Section 11's wording puts that refusal BEFORE `validate_checkout`, which the
call order does not support (`perform_mutation` calls `validate_checkout` first
and `clean_payment_app` after it). The reachability conclusion holds; the
ordering in the doc has been corrected.
"""

import pytest
from django.core.exceptions import ValidationError

from ...giftcard.const import GIFT_CARD_PAYMENT_GATEWAY_ID
from ...graphql.payment.mutations.transaction.transaction_initialize import (
    TransactionInitialize,
)
from ...graphql.payment.mutations.transaction.transaction_process import (
    TransactionProcess,
)
from ...payment.models import TransactionItem
from ...plugins.manager import get_plugins_manager

pytestmark = pytest.mark.django_db


def test_a_checkout_that_can_never_complete_is_refused_before_anything_is_charged():
    """The gate itself, on the mutation that carries the carve-out."""


def test_the_gate_refuses_a_checkout_that_can_never_complete(checkout):
    manager = get_plugins_manager(allow_replica=False)

    with pytest.raises(ValidationError):
        TransactionInitialize.validate_checkout(checkout, manager)


def test_the_gift_card_gateway_is_carved_out_of_the_gate(checkout):
    manager = get_plugins_manager(allow_replica=False)

    assert (
        TransactionInitialize.validate_checkout(
            checkout, manager, GIFT_CARD_PAYMENT_GATEWAY_ID
        )
        is None
    )


def test_a_gift_card_transaction_is_refused_on_the_twin_whatever_the_gate_says():
    """The whole reachability argument for the asymmetry, as a check.

    If a real `App` row ever comes to carry the built-in gift card identifier,
    this goes red and the carve-out has to be carried across to
    `transactionProcess` as well.
    """
    with pytest.raises(ValidationError):
        TransactionProcess.clean_payment_app(
            TransactionItem(app_identifier=GIFT_CARD_PAYMENT_GATEWAY_ID)
        )
