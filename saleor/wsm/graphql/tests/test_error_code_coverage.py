# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every code this layer can raise is a member of the enum that describes it.

`WsmErrorCode` is what the Dashboard branches on, and it is also the graphene
enum the payload serialises THROUGH: a code raised by a mutation but missing
from the enum is not a lint failure, it is a 500 on the error path, which is
the path nobody demos. The three domains were written on three branches and
each rewrote this enum with its own half, so the union is exactly the kind of
thing a merge resolves wrongly and no per-domain test notices.

Source scan rather than a call-every-mutation test on purpose: a raise that
only fires on a rule no fixture triggers is precisely the one a hand-written
list forgets.
"""

import ast
import importlib
import pathlib

import pytest

from ..errors import WsmErrorCode

GRAPHQL_ROOT = pathlib.Path(__file__).resolve().parent.parent
RAISERS = {"error", "_error", "ValidationError"}


def _modules():
    for path in sorted(GRAPHQL_ROOT.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def _module_name(path: pathlib.Path) -> str:
    parts = path.relative_to(GRAPHQL_ROOT).with_suffix("").parts
    return ".".join(("saleor", "wsm", "graphql", *parts))


def _resolve(node, path):
    """The string behind a `code=` argument, or None if it is not a constant."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    module = importlib.import_module(_module_name(path))
    if isinstance(node, ast.Name):
        return getattr(module, node.id, None)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = getattr(module, node.value.id, None)
        return getattr(owner, node.attr, None) if owner is not None else None
    return None


def _codes(path: pathlib.Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in RAISERS:
            continue
        arguments = [kw.value for kw in node.keywords if kw.arg == "code"]
        if name in {"error", "_error"} and len(node.args) > 1:
            arguments.append(node.args[1])
        for argument in arguments:
            value = _resolve(argument, path)
            if isinstance(value, str):
                found.add(value)
    return found


@pytest.mark.parametrize("path", list(_modules()), ids=lambda p: p.name)
def test_every_code_this_module_raises_is_a_member_of_the_enum(path):
    declared = {code.value for code in WsmErrorCode}
    missing = sorted(_codes(path) - declared)

    assert not missing, (
        f"{path.relative_to(GRAPHQL_ROOT)} raises {missing}, which the Dashboard "
        "cannot receive: add them to WsmErrorCode."
    )


def test_the_scan_reads_the_domains_it_claims_to_read():
    """A scanner that matched nothing would pass the test above forever."""
    seen = set()
    for path in _modules():
        seen |= _codes(path)

    assert {
        "duplicate_tier_group",  # compose
        "duplicate_kit_member",  # containers
        "duplicate_group_code",  # dealer
        "duplicated_input_item",
        "not_found",
    } <= seen
