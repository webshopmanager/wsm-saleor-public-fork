# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Two ways a compose write can damage a row it was told nothing about.

An update that omits a field must leave that field alone, and an id that names
the wrong kind of row must come back as a field error rather than as whatever
assigning a Collection to a product column does.
"""

import graphene
import pytest

from saleor.graphql.tests.utils import get_graphql_content

UPDATE = """
    mutation Update($id: ID!, $input: WsmOptionSetUpdateInput!) {
      wsmOptionSetUpdate(id: $id, input: $input) {
        optionSet { id values { id name skuFragment } }
        errors { field code message }
      }
    }
"""

CREATE = """
    mutation Create($input: WsmOptionSetCreateInput!) {
      wsmOptionSetCreate(input: $input) {
        optionSet { id }
        errors { field code message }
      }
    }
"""

FEE_CREATE = """
    mutation FeeCreate($input: WsmFeeCreateInput!) {
      wsmFeeCreate(input: $input) {
        fee { id }
        errors { field code message }
      }
    }
"""


def test_an_update_that_omits_the_sku_fragment_leaves_the_stored_one_alone(
    merchant_api_client, option_set, black
):
    """A partial row edit must not silently unbuild the SKU the store ships.

    The Dashboard datagrid sends only what the merchant touched. `skuFragment`
    absent means "unchanged"; emptying it renames every SKU this choice builds.
    """
    response = merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmOptionSet", option_set.pk),
            "input": {
                "values": [
                    {
                        "id": graphene.Node.to_global_id("WsmOptionValue", black.pk),
                        "name": "Black",
                        "priceDelta": "12.00",
                    }
                ]
            },
        },
    )
    payload = get_graphql_content(response)["data"]["wsmOptionSetUpdate"]

    assert payload["errors"] == []
    black.refresh_from_db()
    assert black.sku_fragment == "BLK"


def test_an_explicit_empty_sku_fragment_still_clears_it(
    merchant_api_client, option_set, black
):
    """The other half: absent is unchanged, empty string is a deliberate clear."""
    merchant_api_client.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmOptionSet", option_set.pk),
            "input": {
                "values": [
                    {
                        "id": graphene.Node.to_global_id("WsmOptionValue", black.pk),
                        "name": "Black",
                        "skuFragment": "",
                    }
                ]
            },
        },
    )

    black.refresh_from_db()
    assert black.sku_fragment == ""


@pytest.mark.parametrize(
    ("query", "input_"),
    [
        (CREATE, {"name": "Finish"}),
        (FEE_CREATE, {"label": "Crating", "amount": "10.00"}),
    ],
    ids=["option-set", "fee"],
)
def test_a_collection_id_in_the_product_field_is_a_field_error(
    merchant_api_client, collection, query, input_
):
    """Any global id resolves; only a Product id may reach a product column.

    The code is GRAPHQL_ERROR rather than NOT_FOUND because the type mismatch is
    caught while the id is being read, before any row is looked for. Both are
    members of `WsmErrorCode`, and what the Dashboard needs either way is the
    `field`, which is what an untyped resolve loses on its way to a 500.
    """
    payload = get_graphql_content(
        merchant_api_client.post_graphql(
            query,
            {
                "input": dict(
                    input_,
                    product=graphene.Node.to_global_id("Collection", collection.pk),
                )
            },
        )
    )["data"]
    errors = next(iter(payload.values()))["errors"]

    assert [(e["field"], e["code"]) for e in errors] == [("product", "GRAPHQL_ERROR")]
