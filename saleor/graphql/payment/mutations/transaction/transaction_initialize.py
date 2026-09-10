import json
import uuid
from typing import TYPE_CHECKING

import graphene
from django.conf import settings
from django.core.exceptions import ValidationError

from .....app.models import App
from .....channel.models import Channel
from .....checkout import models as checkout_models
from .....checkout.checkout_cleaner import clean_checkout_ready_for_payment
from .....checkout.utils import activate_payments, cancel_active_payments
from .....core.exceptions import PermissionDenied
from .....giftcard.const import GIFT_CARD_PAYMENT_GATEWAY_ID
from .....payment import TransactionItemIdempotencyUniqueError
from .....payment.interface import PaymentGatewayData
from .....payment.utils import handle_transaction_initialize_session
from .....permission.enums import PaymentPermissions
from ....app.dataloaders import get_app_promise
from ....channel.enums import TransactionFlowStrategyEnum
from ....core.doc_category import DOC_CATEGORY_PAYMENTS
from ....core.enums import TransactionInitializeErrorCode
from ....core.scalars import JSON, PositiveDecimal
from ....core.types import common as common_types
from ....plugins.dataloaders import get_plugin_manager_promise
from ...types import TransactionEvent, TransactionItem
from ..base import TransactionSessionBase
from .payment_gateway_initialize import PaymentGatewayToInitialize
from .utils import clean_customer_ip_address

if TYPE_CHECKING:
    from .....plugins.manager import PluginsManager


class TransactionInitialize(TransactionSessionBase):
    transaction = graphene.Field(
        TransactionItem, description="The initialized transaction."
    )
    transaction_event = graphene.Field(
        TransactionEvent,
        description="The event created for the initialized transaction.",
    )
    data = graphene.Field(
        JSON, description="The JSON data required to finalize the payment."
    )

    class Arguments:
        id = graphene.ID(
            description="The ID of the checkout or order.",
            required=True,
        )
        amount = graphene.Argument(
            PositiveDecimal,
            description=(
                "The amount requested for initializing the payment gateway. "
                "If not provided, the difference between checkout.total - "
                "transactions that are already processed will be send."
            ),
        )
        idempotency_key = graphene.String(
            description=(
                "The idempotency key assigned to the action. It will be passed to the "
                "payment app to discover potential duplicate actions. If not provided, "
                "the default one will be generated. If empty string provided, INVALID "
                "error code will be raised."
            )
        )
        action = graphene.Argument(
            TransactionFlowStrategyEnum,
            description=(
                "The expected action called for the transaction. By default, the "
                "`channel.paymentSettings.defaultTransactionFlowStrategy` will be used."
                "The field can be used only by app that has `HANDLE_PAYMENTS` "
                "permission."
            ),
        )
        customer_ip_address = graphene.String(
            description=(
                "The customer's IP address. If not provided Saleor will try to "
                "determine the customer's IP address on its own. "
                "The customer's IP address will be passed to the payment app. "
                "The IP should be in ipv4 or ipv6 format. "
                "The field can be used only by an app that has `HANDLE_PAYMENTS` "
                "permission."
            )
        )
        payment_gateway = graphene.Argument(
            PaymentGatewayToInitialize,
            description="Payment gateway used to initialize the transaction.",
            required=True,
        )

    class Meta:
        doc_category = DOC_CATEGORY_PAYMENTS
        description = (
            "Initializes a transaction session. It triggers the webhook "
            "`TRANSACTION_INITIALIZE_SESSION`, to the requested `paymentGateways`. "
            f"There is a limit of {settings.TRANSACTION_ITEMS_LIMIT} transaction "
            "items per checkout / order."
        )
        error_type_class = common_types.TransactionInitializeError

    @classmethod
    def clean_action(
        cls,
        info,
        action: str | None,
        channel: "Channel",
        payment_gateway: PaymentGatewayData,
    ) -> str:
        if payment_gateway.app_identifier == GIFT_CARD_PAYMENT_GATEWAY_ID:
            return TransactionFlowStrategyEnum.AUTHORIZATION.value

        if not action:
            return channel.default_transaction_flow_strategy
        app = get_app_promise(info.context).get()
        if not app or not app.has_perm(PaymentPermissions.HANDLE_PAYMENTS):
            raise PermissionDenied(permissions=[PaymentPermissions.HANDLE_PAYMENTS])
        return action

    @classmethod
    def clean_app_from_payment_gateway(
        cls, payment_gateway: PaymentGatewayData
    ) -> App | None:
        if payment_gateway.app_identifier == GIFT_CARD_PAYMENT_GATEWAY_ID:
            return None

        app = App.objects.filter(
            identifier=payment_gateway.app_identifier,
            removed_at__isnull=True,
            is_active=True,
        ).first()
        if not app:
            raise ValidationError(
                {
                    "payment_gateway": ValidationError(
                        message="App with provided identifier not found.",
                        code=TransactionInitializeErrorCode.NOT_FOUND.value,
                    )
                }
            )
        return app

    @classmethod
    def clean_idempotency_key(cls, idempotency_key: str | None):
        if not idempotency_key and isinstance(idempotency_key, str):
            raise ValidationError(
                {
                    "idempotency_key": ValidationError(
                        message="Cannot be provided as an empty string.",
                        code=TransactionInitializeErrorCode.INVALID.value,
                    )
                }
            )
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        return idempotency_key

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls,
        root,
        info,
        *,
        id,
        payment_gateway,
        amount=None,
        action=None,
        customer_ip_address=None,
        idempotency_key=None,
    ):
        manager = get_plugin_manager_promise(info.context).get()
        payment_gateway_data = PaymentGatewayData(
            app_identifier=payment_gateway["id"], data=payment_gateway.get("data")
        )
        source_object = cls.clean_source_object(
            info,
            id,
            TransactionInitializeErrorCode.INVALID.value,
            TransactionInitializeErrorCode.NOT_FOUND.value,
            manager=manager,
        )
        checkout_payment_snapshot = None
        if isinstance(source_object, checkout_models.Checkout):
            checkout_payment_snapshot = cls.validate_checkout(
                source_object, manager, payment_gateway_data.app_identifier
            )

        idempotency_key = cls.clean_idempotency_key(idempotency_key)
        action = cls.clean_action(
            info, action, source_object.channel, payment_gateway_data
        )
        customer_ip_address = clean_customer_ip_address(
            info,
            customer_ip_address,
            error_code=TransactionInitializeErrorCode.INVALID.value,
        )

        amount = cls.get_amount(
            source_object,
            amount,
        )
        app = cls.clean_app_from_payment_gateway(payment_gateway_data)
        payment_ids = []
        if isinstance(source_object, checkout_models.Checkout):
            # Deactivate active payment objects to avoid processing checkout
            # with use of two different flows.
            payment_ids = cancel_active_payments(source_object)
        try:
            transaction, event, data = handle_transaction_initialize_session(
                source_object=source_object,
                payment_gateway_data=payment_gateway_data,
                amount=amount,
                action=action,
                customer_ip_address=customer_ip_address,
                app=app,
                manager=manager,
                idempotency_key=idempotency_key,
            )
        except TransactionItemIdempotencyUniqueError as e:
            if payment_ids:
                activate_payments(payment_ids)
            raise ValidationError(
                {
                    "idempotency_key": ValidationError(
                        message=(
                            "Different transaction with provided idempotency key "
                            "already exists."
                        ),
                        code=TransactionInitializeErrorCode.UNIQUE.value,
                    )
                }
            ) from e

        if checkout_payment_snapshot is not None:
            # Metadata values are always strings (see MetadataItem.value in the
            # GraphQL schema) — storing the dict directly would serialize as
            # Python's repr() through GraphQL, not valid JSON.
            transaction.store_value_in_private_metadata(
                {
                    "checkout_payment_snapshot_initialize": json.dumps(
                        checkout_payment_snapshot
                    )
                }
            )
            transaction.save(update_fields=["private_metadata"])

        return cls(transaction=transaction, transaction_event=event, data=data)

    @staticmethod
    def validate_checkout(
        checkout: checkout_models.Checkout,
        manager: "PluginsManager",
        app_identifier: str | None = None,
    ) -> dict | None:
        if checkout.is_checkout_locked():
            error_code = (
                TransactionInitializeErrorCode.CHECKOUT_COMPLETION_IN_PROGRESS.value
            )
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Transaction cannot be initialized - the checkout completion "
                        "is currently in progress. Please wait until the process is "
                        f"finished (max {settings.CHECKOUT_COMPLETION_LOCK_TIME} "
                        "seconds).",
                        code=error_code,
                    )
                }
            )
        if app_identifier == GIFT_CARD_PAYMENT_GATEWAY_ID:
            # WSM-FORK: the readiness gate below exists to stop an external
            # payment APP from charging real money against a checkout that can
            # never become an order. The gift card gateway is not one of those.
            # `clean_app_from_payment_gateway` above returns None for this
            # identifier precisely because there is no app: Saleor is the
            # gateway, the balance it moves is Saleor's own ledger, and Saleor
            # releases and detaches that authorization itself when the checkout
            # does not complete (which is exactly what the two
            # `test_gift_card_detach_gift_card_from_checkout_*` e2e tests
            # prove). Attaching a card to a checkout that is not payable yet is
            # the ordinary flow, so gating it refuses a call upstream accepts
            # and strands nothing by allowing it. No snapshot either: there is
            # no outside charge for a later total to be reconciled against.
            return None
        # A charge attempted against a checkout that can never actually
        # complete (no shipping method, no billing address, no lines) risks
        # real money charged with no order and no way to recover
        # automatically: refuse before any payment app is ever asked to
        # charge, rather than discovering it after the fact.
        return clean_checkout_ready_for_payment(
            checkout, manager, TransactionInitializeErrorCode.INVALID.value
        )
