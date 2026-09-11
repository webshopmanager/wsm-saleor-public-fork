# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The composed schema is stock's schema plus ours, and says so out loud."""

from ....graphql import api
from .. import schema as wsm_schema


def test_the_pinned_digest_is_the_api_block_this_layer_was_written_against():
    """An upstream bump that changes how the schema is built is a red test.

    Not a style check: `wsm/graphql/schema.py` repeats api.py's
    `build_federated_schema` call, so a new `types=` entry upstream would give
    stock's schema a type and ours none, with nothing else complaining.
    """
    assert wsm_schema.source_digest() == wsm_schema.API_SCHEMA_SOURCE, (
        "saleor/graphql/api.py's schema-construction block has changed. Read "
        "the diff, mirror any new argument in saleor/wsm/graphql/schema.py, "
        "then re-pin API_SCHEMA_SOURCE."
    )


def test_the_tripwire_moves_when_an_argument_does():
    """The bite check, kept in the suite rather than done once by hand."""
    real = api_source()
    changed = real.replace("subscription=Subscription", "subscription=None")

    assert changed != real, "the fixture no longer resembles api.py"
    assert wsm_schema.source_digest(changed) != wsm_schema.source_digest(real)


def api_source():
    import inspect

    return inspect.getsource(api)


def test_our_fields_are_on_our_schema_and_stock_is_left_alone():
    """Composition, not mutation: api.py's own schema never grew a WSM field."""
    ours = wsm_schema.schema.get_query_type().fields
    theirs = api.schema.get_query_type().fields

    assert "wsmDealerSettings" in ours
    assert "wsmDealerSettings" not in theirs
    assert "wsmDealerSettingsUpdate" in wsm_schema.schema.get_mutation_type().fields


def test_every_stock_root_field_survived_the_composition():
    """Subclassing must ADD. A name lost here is a client broken in production."""
    ours = set(wsm_schema.schema.get_query_type().fields)
    theirs = set(api.schema.get_query_type().fields)

    assert theirs <= ours, sorted(theirs - ours)

    our_mutations = set(wsm_schema.schema.get_mutation_type().fields)
    their_mutations = set(api.schema.get_mutation_type().fields)
    assert their_mutations <= our_mutations, sorted(their_mutations - our_mutations)
