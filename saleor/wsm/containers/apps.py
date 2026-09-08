# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.apps import AppConfig


class ContainersConfig(AppConfig):
    name = "saleor.wsm.containers"
    # Explicit, because Django would otherwise label this app "containers" and
    # the tables would lose the wsm_containers_ prefix that keeps them out of
    # core's namespace.
    label = "wsm_containers"
    verbose_name = "WSM Containers"
    default_auto_field = "django.db.models.BigAutoField"
