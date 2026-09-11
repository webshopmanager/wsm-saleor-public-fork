# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One column: the dealers who are never offered shipping that costs nothing.

Additive and defaulted, so an existing row comes out of this exactly as it went
in, shipping like every other shopper. 45 tenants carry `customer.nofreeshipping`
on 11,272 accounts, ds 4 of them and lsl 6 (measured 2026-09-11).

MERGE-ORDER NOTE. This sits on top of `wsm/dealer-terms-checkout`, which takes
`0004`, while `wsm/dealer-gated-catalogue` takes `0003` against the same base.
Whichever of those lands second repoints its own `dependencies`; this file
follows `0004` and moves with it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("wsm_dealer", "0004_terms_checkout"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealercustomer",
            name="no_free_shipping",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Never offer this dealer a shipping method that costs nothing. "
                    "Free shipping is something you sell retail shoppers; a dealer on "
                    "this flag pays their own freight, or has it billed to their "
                    "carrier account. Off by default, so a dealer ships like everyone "
                    "else until you say otherwise."
                ),
            ),
        ),
    ]
