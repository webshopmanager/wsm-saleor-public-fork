# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One row in `wsm_dealer_dealersettings`, asserted by the database.

Additive and state-safe: one `AddConstraint` on a fork table, no data touched
and no core table named. The constraint is `pk = 1`, so an install that somehow
holds a row at another key fails this migration LOUDLY rather than being
silently renumbered, which is the right failure mode for the toggle that decides
whether a voucher stacks on a dealer price.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wsm_dealer", "0001_initial")]

    operations = [
        migrations.AddConstraint(
            model_name="dealersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(pk=1),
                name="wsm_dealer_settings_is_one_row",
            ),
        ),
    ]
