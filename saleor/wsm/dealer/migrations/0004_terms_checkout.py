# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What a dealer ACCOUNT carries beyond its prices, so ds can buy on terms.

Four columns, all additive, all defaulted, no data migration: an existing row
comes out of this exactly as it went in, not enabled for terms and not on hold.
That is the same default-deny the rest of this app takes.

MERGE-ORDER NOTE. `wsm/dealer-gated-catalogue` is in flight against the same
base and takes `0003`. Both branches therefore depend on `0002` and whichever
lands SECOND has two leaf migrations; the fix is one line, repointing that
branch's `dependencies` at the other's file, and `makemigrations --check`
reddens until it is done.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("wsm_dealer", "0002_dealersettings_is_one_row"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealercustomer",
            name="invoice_payment",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Let this shopper place orders without paying, to be "
                    "invoiced. They get a second choice at the payment step, "
                    "Pay on account, and the order arrives authorized and "
                    "unpaid. Off by default: an account nobody has approved "
                    "for terms pays like everyone else."
                ),
            ),
        ),
        migrations.AddField(
            model_name="dealercustomer",
            name="account_number",
            field=models.CharField(
                blank=True,
                max_length=100,
                help_text=(
                    "This dealer's account number in your own books or ERP. "
                    "Printed on their orders so an invoice can be matched "
                    "without a lookup."
                ),
            ),
        ),
        migrations.AddField(
            model_name="dealercustomer",
            name="account_status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("probation", "Probation"),
                    ("hold", "Hold"),
                ],
                default="active",
                max_length=10,
                help_text=(
                    "Hold stops this shopper placing ANY order, by card as well "
                    "as on account, until you set it back to Active. It does "
                    "not touch their sign-in, their prices or their past "
                    "orders. Probation is a note to yourself: it buys exactly "
                    "as Active does."
                ),
            ),
        ),
        migrations.AddField(
            model_name="dealersettings",
            name="po_label",
            field=models.CharField(
                default="PO number",
                max_length=50,
                help_text=(
                    "What you call the reference on an account order. Shown "
                    "above the box at checkout and on the order. ds calls it "
                    "one thing, another merchant calls it a job number or a "
                    'release; the default is "PO number".'
                ),
            ),
        ),
        migrations.AddField(
            model_name="dealersettings",
            name="po_required",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "On: a shopper paying on account must give a purchase order "
                    "number before the order can be placed. Off (the default): "
                    "the box is still offered and still stored, it is just not "
                    "demanded. 27 of the 108 tenants that invoice on 5.0 "
                    "require one."
                ),
            ),
        ),
    ]
