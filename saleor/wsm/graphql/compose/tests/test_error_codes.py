# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Every `code=` compose raises is a member of the enum the schema declares.

`get_error_code_from_error` passes an unrecognised code straight through, so a
rule raised with a code the enum does not carry reaches the Dashboard as an
enum value the schema cannot serialise: a 500 on the screen that was supposed
to render a merchant sentence. This test reads the raises themselves rather
than a hand-kept list, so a NEW rule with a new code is red here on the day it
is written and not on the day a merchant trips it.
"""

import inspect
import re

from saleor.wsm.compose import models
from saleor.wsm.graphql.compose import mutations
from saleor.wsm.graphql.errors import WsmErrorCode

# `code=SOME_CONSTANT` on a raise, which is the shape every compose rule uses.
CODE_ON_A_RAISE = re.compile(r"code=(?:models\.)?([A-Z][A-Z0-9_]+)[,)\n]")


def compose_codes():
    """Both places a compose rule is raised from, read as source.

    Eight rules live on the models. `DUPLICATE_TIER_GROUP` is raised by the
    mutation instead, because the constraint behind it fires in the database as
    an IntegrityError once the first duplicate is written, which is a 500 and
    not a field error; its constant still lives beside the others in
    `compose/models.py`.
    """
    source = inspect.getsource(models) + inspect.getsource(mutations)
    return sorted(set(CODE_ON_A_RAISE.findall(source)))


def test_the_scan_found_the_raises_it_is_meant_to_check():
    """A check with nothing to do is not a pass: name the floor it stands on."""
    assert len(compose_codes()) >= 9, compose_codes()


def test_every_compose_error_code_is_declared():
    declared = {member.value for member in WsmErrorCode}

    for name in compose_codes():
        value = getattr(models, name, None)
        assert isinstance(value, str), (
            f"compose/models.py raises with code={name}, which is not a string "
            f"constant in that module."
        )
        assert value in declared, (
            f"{name} = {value!r} is raised by compose and is not a member of "
            f"WsmErrorCode, so the Dashboard would receive an enum value the "
            f"schema does not declare."
        )
