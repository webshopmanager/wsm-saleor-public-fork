# WSM-FORK: fork-owned file. See FORK-NOTES.md.
from django.apps import AppConfig


class WsmConfig(AppConfig):
    name = "saleor.wsm"
    label = "wsm"
    verbose_name = "WSM fork additions"

    def ready(self):
        # Must land before graphene builds the schema, which happens on the first
        # import of `saleor.graphql.api`, i.e. after `django.setup()`.
        from . import category_products

        category_products.patch()
