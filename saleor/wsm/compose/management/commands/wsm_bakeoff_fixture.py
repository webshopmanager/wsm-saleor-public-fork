# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The bake-off's catalog fixture and its merchant. Idempotent: re-running is a no-op.

Creates only what acceptance bars B1 and B2 need: two configurable products with
a price, a channel listing, stock, and the non-superuser staff account the
merchant walk is done as. The option sets, values, tier rows and fees are NOT
created here on purpose: a merchant creating them by hand through the admin IS
the B1 test.
"""

import os
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

MERCHANT_EMAIL = "merchant@bakeoff.test"
WAREHOUSE_SLUG = "bakeoff-warehouse"

PRODUCTS = [
    ("L600084", "Stage 2 Kit", Decimal("3998.99")),
    ("L600088", "Stage 3 Kit", Decimal("6399.00")),
]


class Command(BaseCommand):
    help = "Create the bake-off fixture products and the non-superuser merchant."

    @transaction.atomic
    def handle(self, *args, **options):
        from django.contrib.auth import get_permission_codename
        from django.contrib.contenttypes.models import ContentType

        from saleor.account.models import Address, User
        from saleor.channel.models import Channel
        from saleor.permission.models import Permission
        from saleor.product import ProductTypeKind
        from saleor.product.models import (
            Product,
            ProductChannelListing,
            ProductType,
            ProductVariant,
            ProductVariantChannelListing,
        )
        from saleor.warehouse.models import Stock, Warehouse
        from saleor.wsm.compose.models import (
            DealerTierOptionPrice,
            Fee,
            OptionSet,
            OptionValue,
        )

        channel = Channel.objects.filter(
            slug=os.environ.get("DEFAULT_CHANNEL_SLUG", "default-channel")
        ).first()
        if channel is None:
            raise CommandError("no default channel; run `populatedb` first")

        product_type, _ = ProductType.objects.get_or_create(
            slug="wsm-bakeoff-kit",
            defaults={
                "name": "Bake-off Kit",
                "kind": ProductTypeKind.NORMAL,
                "has_variants": False,
                "is_shipping_required": True,
            },
        )

        address, _ = Address.objects.get_or_create(
            company_name="WSM Bake-off",
            street_address_1="1 Bake-off Way",
            city="SAN DIEGO",
            country_area="CA",
            postal_code="92101",
            country="US",
        )
        warehouse, _ = Warehouse.objects.get_or_create(
            slug=WAREHOUSE_SLUG,
            defaults={"name": "Bake-off Warehouse", "address": address},
        )
        warehouse.channels.add(channel)

        now = timezone.now()
        for sku, name, price in PRODUCTS:
            product, _ = Product.objects.get_or_create(
                slug=sku.lower(),
                defaults={"name": name, "product_type": product_type},
            )
            ProductChannelListing.objects.update_or_create(
                product=product,
                channel=channel,
                defaults={
                    "is_published": True,
                    "published_at": now,
                    "visible_in_listings": True,
                    "available_for_purchase_at": now,
                    "currency": channel.currency_code,
                    "discounted_price_amount": price,
                },
            )
            variant, _ = ProductVariant.objects.get_or_create(
                sku=sku, defaults={"product": product, "name": name}
            )
            ProductVariantChannelListing.objects.update_or_create(
                variant=variant,
                channel=channel,
                defaults={
                    "currency": channel.currency_code,
                    "price_amount": price,
                    "discounted_price_amount": price,
                },
            )
            Stock.objects.get_or_create(
                warehouse=warehouse,
                product_variant=variant,
                defaults={"quantity": 10},
            )
            self.stdout.write(f"product {sku} at {price} {channel.currency_code}")

        password = os.environ.get("MERCHANT_PASSWORD")
        if not password:
            raise CommandError("MERCHANT_PASSWORD is not set in the environment")

        merchant, created = User.objects.get_or_create(
            email=MERCHANT_EMAIL,
            defaults={"first_name": "Bake-off", "last_name": "Merchant"},
        )
        merchant.is_staff = True
        merchant.is_active = True
        # The whole point of B1: a superuser walk proves nothing about what a
        # merchant can actually reach.
        merchant.is_superuser = False
        merchant.set_password(password)
        merchant.save()

        codenames = []
        for model in (OptionSet, OptionValue, DealerTierOptionPrice, Fee):
            opts = model._meta
            codenames += [
                get_permission_codename(action, opts)
                for action in opts.default_permissions
            ]
        content_types = ContentType.objects.filter(app_label="wsm_compose")
        perms = Permission.objects.filter(
            content_type__in=content_types, codename__in=codenames
        )
        if not perms.exists():
            raise CommandError(
                "wsm_compose permissions do not exist; run `migrate` to create them"
            )
        merchant.user_permissions.set(perms)

        self.stdout.write(
            f"merchant {MERCHANT_EMAIL} "
            f"({'created' if created else 'updated'}) with {perms.count()} "
            "wsm_compose permissions, is_superuser=False"
        )
