# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.apps import AppConfig


class DealerConfig(AppConfig):
    name = "saleor.wsm.dealer"
    # Explicit, for the same reason as wsm.compose: Django would otherwise label
    # this app "dealer" and the tables would lose the wsm_dealer_ prefix that
    # keeps them out of core's namespace.
    label = "wsm_dealer"
    verbose_name = "WSM Dealer Pricing"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Requirement 2.4: no discount combines with dealer pricing unless the
        # merchant turns stacking on. Stock Saleor has no per-line discount
        # exclusion, so this installs one. See no_stacking.py and the
        # "Monkey patches" heading in docs/wsm/CORE-TOUCHES.md.
        from . import no_stacking

        no_stacking.install()
