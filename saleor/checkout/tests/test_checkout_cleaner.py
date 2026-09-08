import pytest

from ...core.exceptions import GiftCardNotApplicable
from ..checkout_cleaner import _validate_gift_cards, diff_checkout_payment_snapshot


def test_validate_gift_cards_rejects_mismatched_assignment(
    checkout_with_gift_card, customer_user, staff_user
):
    # given
    checkout = checkout_with_gift_card
    checkout.user = staff_user
    checkout.save(update_fields=["user"])
    gift_card = checkout.gift_cards.first()
    gift_card.assigned_to = customer_user
    gift_card.assigned_to_email = customer_user.email
    gift_card.save(update_fields=["assigned_to", "assigned_to_email"])

    # when / then
    with pytest.raises(GiftCardNotApplicable):
        _validate_gift_cards(checkout)


def test_validate_gift_cards_allows_matching_assignment(
    checkout_with_gift_card, customer_user
):
    # given
    checkout = checkout_with_gift_card
    checkout.user = customer_user
    checkout.save(update_fields=["user"])
    gift_card = checkout.gift_cards.first()
    gift_card.assigned_to = customer_user
    gift_card.assigned_to_email = customer_user.email
    gift_card.save(update_fields=["assigned_to", "assigned_to_email"])

    # when / then (no raise)
    _validate_gift_cards(checkout)


def _snapshot(
    *,
    lines=None,
    total_gross_amount="54.20",
    total_net_amount="50.00",
    shipping_method_name="EMS",
    shipping_price_gross_amount="34.20",
):
    return {
        "snapshotted_at": "2026-07-31T00:00:00+00:00",
        "checkout_id": "Q2hlY2tvdXQ6MQ==",
        "currency": "USD",
        "total_gross_amount": total_gross_amount,
        "total_net_amount": total_net_amount,
        "lines": (
            lines
            if lines is not None
            else [
                {
                    "variant_id": "UHJvZHVjdFZhcmlhbnQ6MQ==",
                    "variant_sku": "TEE-1",
                    "product_name": "Monospace Tee",
                    "variant_name": "M",
                    "quantity": 1,
                }
            ]
        ),
        "shipping_method_name": shipping_method_name,
        "shipping_price_gross_amount": shipping_price_gross_amount,
    }


def test_diff_checkout_payment_snapshot_no_change_returns_none():
    paid_for = _snapshot()
    current = _snapshot()

    assert diff_checkout_payment_snapshot(paid_for, current) is None


def test_diff_checkout_payment_snapshot_detects_quantity_change():
    paid_for = _snapshot()
    current = _snapshot(
        lines=[
            {
                "variant_id": "UHJvZHVjdFZhcmlhbnQ6MQ==",
                "variant_sku": "TEE-1",
                "product_name": "Monospace Tee",
                "variant_name": "M",
                "quantity": 3,
            }
        ]
    )

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is True
    assert "TEE-1: 1x -> 3x" in diff["message"]


def test_diff_checkout_payment_snapshot_detects_added_line():
    paid_for = _snapshot()
    current = _snapshot(
        lines=[
            *_snapshot()["lines"],
            {
                "variant_id": "UHJvZHVjdFZhcmlhbnQ6Mg==",
                "variant_sku": "MUG-1",
                "product_name": "Mighty Mug",
                "variant_name": "Default",
                "quantity": 2,
            },
        ]
    )

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is True
    assert "added 2x MUG-1" in diff["message"]


def test_diff_checkout_payment_snapshot_detects_removed_line():
    paid_for = _snapshot(
        lines=[
            *_snapshot()["lines"],
            {
                "variant_id": "UHJvZHVjdFZhcmlhbnQ6Mg==",
                "variant_sku": "MUG-1",
                "product_name": "Mighty Mug",
                "variant_name": "Default",
                "quantity": 2,
            },
        ]
    )
    current = _snapshot()

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is True
    assert "removed 2x MUG-1" in diff["message"]


def test_diff_checkout_payment_snapshot_shipping_price_change_is_not_content_changed():
    paid_for = _snapshot(
        shipping_method_name="EMS", shipping_price_gross_amount="34.20"
    )
    current = _snapshot(shipping_method_name="DHL", shipping_price_gross_amount="66.19")

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is False
    assert "shipping changed: EMS ($34.20) -> DHL ($66.19)" in diff["message"]


def test_diff_checkout_payment_snapshot_shipping_method_change_same_price():
    paid_for = _snapshot(
        shipping_method_name="EMS", shipping_price_gross_amount="34.20"
    )
    current = _snapshot(
        shipping_method_name="EMS Replacement", shipping_price_gross_amount="34.20"
    )

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is False
    assert "shipping method changed: EMS -> EMS Replacement" in diff["message"]


def test_diff_checkout_payment_snapshot_tax_change_is_not_content_changed():
    paid_for = _snapshot(total_gross_amount="54.20", total_net_amount="50.00")
    current = _snapshot(total_gross_amount="56.00", total_net_amount="50.00")

    diff = diff_checkout_payment_snapshot(paid_for, current)

    assert diff is not None
    assert diff["content_changed"] is False
    assert "tax changed" in diff["message"]


def test_diff_checkout_payment_snapshot_sub_cent_drift_is_ignored():
    paid_for = _snapshot(shipping_price_gross_amount="34.20")
    current = _snapshot(shipping_price_gross_amount="34.205")

    assert diff_checkout_payment_snapshot(paid_for, current) is None


def test_diff_checkout_payment_snapshot_missing_shipping_handled_gracefully():
    paid_for = _snapshot(shipping_method_name=None, shipping_price_gross_amount=None)
    current = _snapshot(shipping_method_name=None, shipping_price_gross_amount=None)

    assert diff_checkout_payment_snapshot(paid_for, current) is None
