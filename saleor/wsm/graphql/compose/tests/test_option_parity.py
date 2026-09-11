# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Parity wave A over the wire: help text, the default, the deselect prompt.

Posted to `reverse("api")` like every other test in this package, because a
resolver called by hand cannot tell the difference between a field that exists
and a field that is SERVED, and the Dashboard reads these over the wire.
"""

import graphene
import pytest

from saleor.graphql.tests.utils import get_graphql_content
from saleor.wsm.compose import pricing
from saleor.wsm.compose.models import INVALID, OptionSet, OptionValue

CREATE = """
    mutation WsmOptionSetCreate($input: WsmOptionSetCreateInput!) {
      wsmOptionSetCreate(input: $input) {
        optionSet {
          id
          deselectPrompt
          values { id name helpText isDefault priceDelta }
        }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation WsmOptionSetUpdate($id: ID!, $input: WsmOptionSetUpdateInput!) {
      wsmOptionSetUpdate(id: $id, input: $input) {
        optionSet {
          id
          deselectPrompt
          values { id name helpText isDefault }
        }
        errors { field code message }
      }
    }
"""

DETAIL = """
    query WsmOptionSet($id: ID!) {
      wsmOptionSet(id: $id) {
        deselectPrompt
        values { name helpText isDefault }
      }
    }
"""


def _create(client, product, values, deselect_prompt=""):
    return client.post_graphql(
        CREATE,
        {
            "input": {
                "product": graphene.Node.to_global_id("Product", product.pk),
                "name": "Tuner",
                "promptType": "CHOICE_ONE",
                "deselectPrompt": deselect_prompt,
                "values": values,
            }
        },
    )


def test_create_round_trips_all_three_columns(merchant_api_client, product):
    response = _create(
        merchant_api_client,
        product,
        [
            {"name": "No tuner", "priceDelta": "0"},
            {
                "name": "Stage 2 tuner",
                "priceDelta": "250.00",
                "helpText": "Fits 2019 and newer only",
                "isDefault": True,
            },
        ],
        deselect_prompt="Skip the tuner",
    )

    data = get_graphql_content(response)["data"]["wsmOptionSetCreate"]
    assert data["errors"] == []
    assert data["optionSet"]["deselectPrompt"] == "Skip the tuner"
    assert data["optionSet"]["values"] == [
        {
            "id": data["optionSet"]["values"][0]["id"],
            "name": "No tuner",
            "helpText": "",
            "isDefault": False,
            "priceDelta": 0.0,
        },
        {
            "id": data["optionSet"]["values"][1]["id"],
            "name": "Stage 2 tuner",
            "helpText": "Fits 2019 and newer only",
            "isDefault": True,
            "priceDelta": 250.0,
        },
    ]
    stored = OptionSet.objects.get(name="Tuner")
    assert stored.deselect_prompt == "Skip the tuner"
    assert stored.default_value.name == "Stage 2 tuner"
    assert stored.default_value.help_text == "Fits 2019 and newer only"


def test_a_second_default_in_one_submit_is_a_field_error_on_that_row(
    merchant_api_client, product
):
    response = _create(
        merchant_api_client,
        product,
        [
            {"name": "No tuner", "isDefault": True},
            {"name": "Stage 2 tuner", "isDefault": True},
        ],
    )

    errors = get_graphql_content(response)["data"]["wsmOptionSetCreate"]["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [
        ("values.1.isDefault", INVALID.upper())
    ]
    assert "No tuner" in errors[0]["message"]
    # The whole submit unwound: nothing half-written behind a refusal.
    assert not OptionSet.objects.filter(name="Tuner").exists()


def test_update_moves_the_default_between_two_stored_rows(
    merchant_api_client, option_set, black
):
    black.is_default = True
    black.save(update_fields=["is_default"])
    raw = option_set.values.get(name="Raw")

    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmOptionSet", option_set.pk),
            "input": {
                "deselectPrompt": "No finish, thanks",
                "values": [
                    {
                        "id": graphene.Node.to_global_id("WsmOptionValue", black.pk),
                        "name": "Black",
                        "isDefault": False,
                    },
                    {
                        "id": graphene.Node.to_global_id("WsmOptionValue", raw.pk),
                        "name": "Raw",
                        "isDefault": True,
                        "helpText": "Paint it yourself",
                    },
                ],
            },
        },
    )

    data = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]
    assert data["errors"] == []
    option_set.refresh_from_db()
    assert option_set.deselect_prompt == "No finish, thanks"
    assert option_set.default_value.name == "Raw"
    assert option_set.default_value.help_text == "Paint it yourself"


def test_an_update_that_never_mentions_the_columns_leaves_them_alone(
    merchant_api_client, option_set, black
):
    """The datagrid sends only what the merchant touched (`mutations.py:184`)."""
    black.is_default = True
    black.help_text = "The one everybody buys"
    black.save(update_fields=["is_default", "help_text"])

    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmOptionSet", option_set.pk),
            "input": {
                "values": [
                    {
                        "id": graphene.Node.to_global_id("WsmOptionValue", black.pk),
                        "name": "Black",
                    }
                ]
            },
        },
    )

    assert get_graphql_content(response)["data"]["wsmOptionSetUpdate"]["errors"] == []
    black.refresh_from_db()
    assert black.is_default is True
    assert black.help_text == "The one everybody buys"


def test_the_query_serves_the_three_columns(merchant_api_client, option_set, black):
    black.help_text = "Fits 2019 and newer only"
    black.is_default = True
    black.save(update_fields=["help_text", "is_default"])
    option_set.deselect_prompt = "No finish, thanks"
    option_set.save(update_fields=["deselect_prompt"])

    response = merchant_api_client.post_graphql(
        DETAIL, {"id": graphene.Node.to_global_id("WsmOptionSet", option_set.pk)}
    )

    data = get_graphql_content(response)["data"]["wsmOptionSet"]
    assert data["deselectPrompt"] == "No finish, thanks"
    assert data["values"] == [
        {"name": "Black", "helpText": "Fits 2019 and newer only", "isDefault": True},
        {"name": "Raw", "helpText": "", "isDefault": False},
    ]
