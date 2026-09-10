# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Django serves /static/ itself only on a box that says it has to."""

from ..urls import static_routes


def test_django_does_not_serve_static_by_default(monkeypatch):
    """The target deployment is ECS behind a CDN, where this is a defect."""
    monkeypatch.delenv("WSM_SERVE_STATIC", raising=False)

    assert static_routes() == []


def test_the_one_box_that_needs_it_asks_for_it(monkeypatch):
    """A bake-off box runs DEBUG off with nothing in front of uvicorn."""
    monkeypatch.setenv("WSM_SERVE_STATIC", "true")

    assert [route.name for route in static_routes()] == ["wsm-static"]
