# WSM-FORK: fork-owned file. See FORK-NOTES.md.
from django.db import migrations, models


class Migration(migrations.Migration):
    """State-only. `managed = False`, so this emits no DDL.

    The table is owned by the PartsLogic service, which creates it with its own
    (Go) migrations. This exists so `makemigrations --check` stays clean, and it
    lives in the fork-only `wsm` app rather than in `product` so it can never
    collide with an upstream migration number.
    """

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProductSecondaryCategory",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("product_id", models.BigIntegerField()),
                ("category_id", models.BigIntegerField()),
            ],
            options={
                "db_table": "product_secondary_categories",
                "managed": False,
            },
        ),
    ]
