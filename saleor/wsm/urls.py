# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every URL the fork serves, behind the single ``include`` in saleor/urls.py.

U2 (compose), U3 (dealer) and U4 (containers) add themselves here, not to core's
urls.py, so the core touch stays at one line for the life of the branch. Each
unit that lands adds its own line and nothing else: the merge is a list append.
"""

from django.urls import include, re_path

urlpatterns = [
    re_path(r"^wsm/containers/", include("saleor.wsm.containers.urls")),
]
