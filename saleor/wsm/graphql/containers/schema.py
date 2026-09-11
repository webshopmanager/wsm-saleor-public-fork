# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Containers' queries and mutations, as two mixins."""

import graphene

from ....graphql.core import ResolveInfo
from ....graphql.core.connection import (
    create_connection_slice,
    filter_connection_queryset,
)
from ....graphql.core.context import get_database_connection_name
from ....graphql.core.fields import FilterConnectionField, PermissionsField
from ....graphql.core.utils import from_global_id_or_error
from ....graphql.core.validators import validate_one_of_args_is_in_query
from ....permission.enums import ProductPermissions
from ...containers import models
from ..types import DOC_CATEGORY_WSM
from .filters import WsmKitConfigFilterInput, WsmSeriesConfigFilterInput
from .mutations import (
    WsmKitConfigCreate,
    WsmKitConfigDelete,
    WsmKitConfigUpdate,
    WsmSeriesConfigDelete,
    WsmSeriesConfigUpdate,
)
from .types import (
    WsmKitConfig,
    WsmKitConfigCountableConnection,
    WsmSeriesConfig,
    WsmSeriesConfigCountableConnection,
)

CONTAINER_PERMISSIONS = [ProductPermissions.MANAGE_PRODUCTS]


def _by_id_or_collection(info: ResolveInfo, manager, type_name, id, collection):
    """One detail resolver for both container rows, keyed either way.

    A merchant screen opens on a collection and a Dashboard list links by row
    id, so refusing one of the two would cost a round trip on every visit.
    Exactly one of them, because answering a query that named both would mean
    picking which key the caller meant.
    """
    validate_one_of_args_is_in_query(
        "id", id, "collection", collection, use_camel_case=True
    )
    # Named connection, not a bare `.objects`: under
    # ENABLE_RESTRICT_WRITER_MIDDLEWARE an unrouted read inside a GraphQL
    # request raises UnsafeWriterAccessError.
    qs = manager.using(get_database_connection_name(info.context)).select_related(
        "collection"
    )
    if id:
        _, pk = from_global_id_or_error(id, type_name, raise_error=True)
        return qs.filter(pk=pk).first()
    _, pk = from_global_id_or_error(collection, "Collection", raise_error=True)
    return qs.filter(collection_id=pk).first()


class WsmContainersQueries(graphene.ObjectType):
    wsm_series_config = PermissionsField(
        WsmSeriesConfig,
        id=graphene.Argument(graphene.ID, description="ID of the series."),
        collection=graphene.Argument(
            graphene.ID, description="ID of the collection the series is on."
        ),
        description="Look up a series by row ID, or by collection.",
        permissions=CONTAINER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_series_configs = FilterConnectionField(
        WsmSeriesConfigCountableConnection,
        filter=WsmSeriesConfigFilterInput(description="Filtering options for series."),
        description="List of series.",
        permissions=CONTAINER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_kit_config = PermissionsField(
        WsmKitConfig,
        id=graphene.Argument(graphene.ID, description="ID of the kit."),
        collection=graphene.Argument(
            graphene.ID, description="ID of the collection sold as a kit."
        ),
        description="Look up a kit by row ID, or by collection.",
        permissions=CONTAINER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )
    wsm_kit_configs = FilterConnectionField(
        WsmKitConfigCountableConnection,
        filter=WsmKitConfigFilterInput(description="Filtering options for kits."),
        description="List of kits.",
        permissions=CONTAINER_PERMISSIONS,
        doc_category=DOC_CATEGORY_WSM,
    )

    @staticmethod
    def resolve_wsm_series_config(
        _root, info: ResolveInfo, /, *, id=None, collection=None
    ):
        return _by_id_or_collection(
            info, models.SeriesConfig.objects, "WsmSeriesConfig", id, collection
        )

    @staticmethod
    def resolve_wsm_series_configs(_root, info: ResolveInfo, /, **kwargs):
        qs = models.SeriesConfig.objects.using(
            get_database_connection_name(info.context)
        ).select_related("collection")
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmSeriesConfigCountableConnection
        )

    @staticmethod
    def resolve_wsm_kit_config(
        _root, info: ResolveInfo, /, *, id=None, collection=None
    ):
        return _by_id_or_collection(
            info, models.KitConfig.objects, "WsmKitConfig", id, collection
        )

    @staticmethod
    def resolve_wsm_kit_configs(_root, info: ResolveInfo, /, **kwargs):
        qs = models.KitConfig.objects.using(
            get_database_connection_name(info.context)
        ).select_related("collection")
        qs = filter_connection_queryset(
            qs, kwargs, allow_replica=info.context.allow_replica
        )
        return create_connection_slice(
            qs, info, kwargs, WsmKitConfigCountableConnection
        )


class WsmContainersMutations(graphene.ObjectType):
    wsm_series_config_update = WsmSeriesConfigUpdate.Field()
    wsm_series_config_delete = WsmSeriesConfigDelete.Field()
    wsm_kit_config_create = WsmKitConfigCreate.Field()
    wsm_kit_config_update = WsmKitConfigUpdate.Field()
    wsm_kit_config_delete = WsmKitConfigDelete.Field()
