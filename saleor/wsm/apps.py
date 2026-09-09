# WSM-FORK: fork-owned file. See FORK-NOTES.md.
from django.apps import AppConfig


class WsmConfig(AppConfig):
    name = "saleor.wsm"
    label = "wsm"
    verbose_name = "WSM fork additions"

    def ready(self):
        # Must land before graphene builds the schema, which happens on the first
        # import of `saleor.graphql.api`, i.e. after `django.setup()`.
        from . import category_products, reprice

        category_products.patch()
        # MP3: no price this fork wrote onto a checkout line outlives the
        # facts it was computed from. See reprice.py and the "Monkey
        # patches" heading in docs/wsm/CORE-TOUCHES.md.
        reprice.install()
