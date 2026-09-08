# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.apps import AppConfig


class ComposeConfig(AppConfig):
    name = "saleor.wsm.compose"
    # Explicit, because Django would otherwise label this app "compose" and the
    # tables would lose the wsm_compose_ prefix that keeps them out of core's
    # namespace.
    label = "wsm_compose"
    verbose_name = "WSM Compose"
    default_auto_field = "django.db.models.BigAutoField"
