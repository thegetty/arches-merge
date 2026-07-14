import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
import re

from arches import __version__ as arches_version
import arches.app.utils.task_management as task_management
from arches.app.etl_modules.base_data_editor import BaseBulkEditor, HttpRequest
from arches.app.etl_modules.decorators import load_data_async
from arches.app.models.models import (
    CardModel,
    CardXNodeXWidget,
    EditLog,
    GeoJSONGeometry,
    LoadEvent,
    Node,
    NodeGroup,
    ResourceXResource,
    User,
)
from arches.app.models.resource import Resource
from arches.app.models.tile import Tile
from arches.app.utils.decorators import user_created_transaction_match
from arches.app.utils.index_database import index_resources_by_transaction
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.core import serializers
from django.db import connection, transaction
from django.db.models import Model
from arches.app.models import models
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from arches.app.datatypes.datatypes import DataTypeFactory
from arches.app.models.graph import Graph
from packaging.version import Version


import arches_merge.tasks as tasks


details = {
    "etlmoduleid": "",
    "name": "Merge Resources",
    "description": "Merge two or more resources into one resource",
    "etl_type": "edit",
    "component": "views/components/etl_modules/Resources_merge",
    "componentname": "Resources_merge",
    "modulename": "Resources_merge.py",
    "classname": "Resourcesmerge",
    "config": {"bgColor": "#f5c60a", "circleColor": "#f9dd6c"},
    "icon": "fa fa-upload",
    "slug": "Resources_merge",
    "helpsortorder": 8,
    "helptemplate": "Resources_merge-help",
    "reversible": True,
}

logger = logging.getLogger(__name__)

ALLOWED_UNDO_DAYS = 7

if Version(arches_version) >= Version("8.0"):
    RXR_FROM_FIELD = "from_resource"
    RXR_TO_FIELD = "to_resource"
    RXR_NODE_FIELD = "node"
    RXR_TILE_FIELD = "tile"
else:
    RXR_FROM_FIELD = "resourceinstanceidfrom"
    RXR_TO_FIELD = "resourceinstanceidto"
    RXR_NODE_FIELD = "nodeid"
    RXR_TILE_FIELD = "tileid"

RXR_LOG_FIELD_ALIASES = {
    RXR_FROM_FIELD: (
        RXR_FROM_FIELD,
        "resourceinstanceidfrom"
        if RXR_FROM_FIELD != "resourceinstanceidfrom"
        else "from_resource",
    ),
    RXR_TO_FIELD: (
        RXR_TO_FIELD,
        "resourceinstanceidto"
        if RXR_TO_FIELD != "resourceinstanceidto"
        else "to_resource",
    ),
}


class MissingRequiredInputError(Exception):
    pass


def log_event_details(loadid: str | None, message: str):
    if not loadid:
        return

    event = LoadEvent.objects.get(loadid=loadid)

    if not event.load_description:
        event.load_description = ""
    event.load_description += message
    event.save()


class Resourcesmerge(BaseBulkEditor):
    loadid: str | None
    userid: str
    user: AbstractUser | AnonymousUser | None
    nodegroups: dict[str, NodeGroup]

    def __init__(self, request: HttpRequest | None = None, loadid: str | None = None):
        super().__init__(request, loadid)
        self.nodegroups = {}
        self._card_label_cache: dict[str, str] = {}
        self._preview_node_order_cache: dict[str, list[str]] = {}
        self._preview_nodegroup_meta_cache: dict[str, dict[str, Any]] = {}
        self._preview_graph_card_order_cache: dict[str, dict[str, int]] = {}

        self.user = request.user if request else None
        self.datatype_factory = DataTypeFactory()
        self.node_lookup = {}

    def _serialize_obj(self, obj: Model) -> str:
        return serializers.serialize("json", [obj])

    def _tile_delete(self, tile: Tile):
        tile.save_edit(
            user=self.user,
            old_value=self._serialize_obj(tile),
            edit_type="tile delete",
            transaction_id=self.loadid,
        )
        _ = super(Tile, tile).delete()

    def _tile_save(self, tile: Tile):
        old_tile = Tile.objects.get(pk=tile.tileid)
        tile.save_edit(
            user=self.user,
            new_value=self._serialize_obj(tile),
            old_value=self._serialize_obj(old_tile),
            edit_type="tile edit",
            transaction_id=self.loadid,
        )
        _ = super(Tile, tile).save()

    def _resource_x_resource_save(self, resource_x_resource: ResourceXResource):
        old_resource_x_resource = ResourceXResource.objects.get(
            pk=resource_x_resource.resourcexid
        )
        edit = EditLog()
        edit.oldvalue = self._serialize_obj(old_resource_x_resource)
        edit.newvalue = self._serialize_obj(resource_x_resource)
        edit.timestamp = datetime.now()
        edit.nodegroupid = getattr(
            resource_x_resource, RXR_NODE_FIELD
        ).nodegroup.nodegroupid
        edit.edittype = "resourcexresource edit"
        edit.transactionid = UUID(self.loadid)
        edit.save()
        resource_x_resource.save()

    def _resource_x_resource_delete(self, resource_x_resource: ResourceXResource):
        edit = EditLog()
        edit.oldvalue = self._serialize_obj(resource_x_resource)
        edit.timestamp = datetime.now()
        edit.edittype = "resourcexresource delete"
        edit.nodegroupid = getattr(
            resource_x_resource, RXR_NODE_FIELD
        ).nodegroup.nodegroupid
        edit.transactionid = UUID(self.loadid)
        edit.save()

        resource_x_resource.delete()

    def _resource_delete(self, resource: Resource):
        edit = EditLog()
        edit.oldvalue = self._serialize_obj(resource)
        edit.timestamp = datetime.now()
        edit.edittype = "delete"
        edit.transactionid = UUID(self.loadid)
        edit.save()

        _ = super(Resource, resource).delete()

    def _get_card_label(self, nodegroupid: str | None) -> str:
        if not nodegroupid:
            return _("Resource data")

        nodegroup_key = str(nodegroupid)
        if nodegroup_key in self._card_label_cache:
            return self._card_label_cache[nodegroup_key]

        label = nodegroup_key
        card = CardModel.objects.filter(nodegroup_id=nodegroupid).first()
        if card and card.name:
            label = str(card.name)

        self._card_label_cache[nodegroup_key] = label
        return label

    def _deserialize_models(self, value: Any) -> list[Model]:
        if not value:
            return []

        serialized = ""
        if isinstance(value, str):
            serialized = value
        elif isinstance(value, list):
            serialized = json.dumps(value)
        elif isinstance(value, dict):
            serialized = json.dumps([value])
        else:
            try:
                serialized = json.dumps(value)
            except TypeError:
                return []

        models_list: list[Model] = []
        try:
            for obj in serializers.deserialize("json", serialized):
                models_list.append(obj.object)
        except Exception:
            return []

        return models_list

    def _extract_related_resource_id(
        self, instance: Model, attr_name: str
    ) -> str | None:
        if not instance:
            return None

        attr_value = getattr(instance, attr_name, None)
        if attr_value:
            if hasattr(attr_value, "resourceinstanceid"):
                return str(attr_value.resourceinstanceid)
            if hasattr(attr_value, "pk"):
                return str(attr_value.pk)
            return str(attr_value)

        attr_id_value = getattr(instance, f"{attr_name}_id", None)
        if attr_id_value:
            return str(attr_id_value)

        return None

    def _get_resource_id_from_log_value(self, value: Any, attr_name: str) -> str | None:
        field_names = RXR_LOG_FIELD_ALIASES.get(attr_name, (attr_name,))
        for obj in self._deserialize_models(value):
            for field_name in field_names:
                resource_id = self._extract_related_resource_id(
                    obj, field_name + "_id"
                )
                if resource_id:
                    return resource_id
        return None

    def _format_edit_log_label(self, log: EditLog) -> str:
        label = (
            log.resourcedisplayname
            if not log.nodegroupid and log.resourcedisplayname
            else self._get_card_label(log.nodegroupid)
        )
        tile_id = str(log.tileinstanceid) if log.tileinstanceid else ""
        return f"{label} ({tile_id[:8]})" if tile_id else label

    def _stringify_change_value(self, value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except TypeError:
                text = str(value)

        return text

    def _summarize_model_instance(self, instance: Model) -> str:
        if hasattr(instance, "data") and getattr(instance, "data"):
            return self._stringify_change_value(instance.data)

        text_fields: list[str] = []
        for attr in (
            "displayname",
            "name",
            "relationshiptype",
            "inverserelationshiptype",
            "notes",
        ):
            value = getattr(instance, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            if value:
                text_fields.append(f"{attr}: {value}")

        if text_fields:
            return "; ".join(text_fields)

        identifier = getattr(instance, "pk", None) or getattr(instance, "id", None)
        return f"ID: {identifier}" if identifier else ""

    def _summarize_value(self, value: Any) -> str:
        models = self._deserialize_models(value)
        for obj in models:
            summary = self._summarize_model_instance(obj)
            if summary:
                return summary

        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return self._stringify_change_value(parsed)
            except json.JSONDecodeError:
                return self._stringify_change_value(value)

        if value:
            return self._stringify_change_value(value)

        return ""

    def _detail_for_category(self, log: EditLog, category: str) -> str:
        preferred_value = (
            (log.oldvalue if category == "deleted" else log.newvalue)
            or log.oldvalue
            or log.newvalue
        )

        detail = self._summarize_value(preferred_value)
        return detail if detail else _("No data recorded")

    def _categorize_edit_log_entry(
        self, base_resource_id: str | None, log: EditLog
    ) -> str | None:
        fallback_categories = {
            "tile edit": "changed",
            "tile delete": "deleted",
            "resourcexresource edit": "changed",
            "resourcexresource delete": "deleted",
            "delete": "deleted",
        }

        base_id = str(base_resource_id) if base_resource_id else None
        log_resource_id = (
            str(log.resourceinstanceid) if log.resourceinstanceid else None
        )
        edittype = (log.edittype or "").lower()

        if edittype == "tile edit":
            if base_id and log_resource_id == base_id:
                previous_owner = self._get_resource_id_from_log_value(
                    log.oldvalue, "resourceinstance"
                )
                if not previous_owner or previous_owner != base_id:
                    return "added"
                return "changed"
            return fallback_categories.get(edittype)

        if edittype == "tile delete":
            impacted_resource = log_resource_id or self._get_resource_id_from_log_value(
                log.oldvalue, "resourceinstance"
            )
            if base_id and impacted_resource == base_id:
                return "deleted"
            return fallback_categories.get(edittype)

        if edittype == "resourcexresource edit":
            new_from = self._get_resource_id_from_log_value(
                log.newvalue, RXR_FROM_FIELD
            )
            new_to = self._get_resource_id_from_log_value(
                log.newvalue, RXR_TO_FIELD
            )
            old_from = self._get_resource_id_from_log_value(
                log.oldvalue, RXR_FROM_FIELD
            )
            old_to = self._get_resource_id_from_log_value(
                log.oldvalue, RXR_TO_FIELD
            )

            new_involvement = any(
                rid == base_id for rid in (new_from, new_to) if rid is not None
            )
            old_involvement = any(
                rid == base_id for rid in (old_from, old_to) if rid is not None
            )

            if new_involvement and not old_involvement:
                return "added"
            if not new_involvement and old_involvement:
                return "deleted"
            if new_involvement and old_involvement:
                return "changed"
            return fallback_categories.get(edittype)

        if edittype == "resourcexresource delete":
            old_from = self._get_resource_id_from_log_value(
                log.oldvalue, RXR_FROM_FIELD
            )
            old_to = self._get_resource_id_from_log_value(
                log.oldvalue, RXR_TO_FIELD
            )
            if base_id and any(
                rid == base_id for rid in (old_from, old_to) if rid is not None
            ):
                return "deleted"
            return fallback_categories.get(edittype)

        if edittype == "delete":
            deleted_resource = self._get_resource_id_from_log_value(
                log.oldvalue, "resourceinstance"
            )
            if base_id and deleted_resource == base_id:
                return "deleted"
            return fallback_categories.get(edittype)

        return fallback_categories.get(edittype)

    def _build_change_summary(
        self, base_resource_id: str | None
    ) -> dict[str, list[dict[str, str]]]:
        summary = {"added": [], "changed": [], "deleted": []}
        seen_labels: dict[str, set[tuple[str, str]]] = {
            "added": set(),
            "changed": set(),
            "deleted": set(),
        }
        if not self.loadid:
            return summary

        edit_logs = (
            EditLog.objects.filter(
                transactionid=self.loadid,
                edittype__in=(
                    "tile edit",
                    "tile delete",
                    "resourcexresource edit",
                    "resourcexresource delete",
                    "delete",
                ),
            )
            .order_by("timestamp")
            .all()
        )

        for log in edit_logs:
            category = self._categorize_edit_log_entry(base_resource_id, log)
            if not category:
                continue

            label = self._format_edit_log_label(log)
            detail = self._detail_for_category(log, category)
            dedupe_key = (label, detail)
            if dedupe_key in seen_labels[category]:
                continue
            if log.edittype == "delete":
                edit_log = "resource"
            elif log.edittype in ("tile delete", "tile edit"):
                edit_log = "tile"
            elif log.edittype in ("resourcexresource delete", "resourcexresource edit"):
                edit_log = "resourcexresource"
            else:
                edit_log = "unknown"
            summary[category].append(
                {"label": label, "detail": detail, "edit_log": edit_log}
            )
            seen_labels[category].add(dedupe_key)

        return summary

    @method_decorator(user_created_transaction_match, name="dispatch")
    def reverse(self, request: HttpRequest, **kwargs):
        success = False
        response = {"success": success, "data": ""}
        loadid = self.loadid if self.loadid else request.POST.get("loadid", "")

        load_event: LoadEvent = LoadEvent.objects.get(loadid=loadid)
        current_datetimte = datetime.now()
        max_undo_timedelta = timedelta(days=ALLOWED_UNDO_DAYS)
        if load_event.load_end_time + max_undo_timedelta < current_datetimte:
            logger.error(
                f"Load is too old to reverse: loadid={loadid} load_end_time={load_event.load_end_time}, current_datetimte={current_datetimte}, max_undo_timedelta={max_undo_timedelta}"
            )
            response["data"] = _("Load is too old to reverse")
            return response

        try:
            if task_management.check_if_celery_available():
                logger.warning(_("Delegating load reversal to Celery task"))
                tasks.reverse_merge_load.apply_async([loadid])
            else:
                self.reverse_load(loadid)
            response["success"] = True
        except Exception as e:
            response["data"] = str(e)
            logger.error(e)
        logger.warning(response)
        return response

    def reverse_load(self, loadid: str):
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE load_event SET status = %s WHERE loadid = %s""",
                ("reversing", loadid),
            )
            resources_changed_count = self.reverse_edit_log_entries(loadid)
            cursor.execute(
                """UPDATE load_event SET status = %s, load_details = load_details::jsonb || ('{"resources_removed":' || %s || '}')::jsonb WHERE loadid = %s""",
                ("unloaded", resources_changed_count, loadid),
            )

    @transaction.atomic()
    def reverse_edit_log_entries(self, transaction_id: str):
        transaction_changes = (
            EditLog.objects.filter(transactionid=transaction_id)
            .order_by("-timestamp")
            .all()
        )
        number_of_db_changes = 0
        for edit_log in transaction_changes:
            if edit_log.edittype not in (
                "delete",
                "tile edit",
                "tile delete",
                "resourcexresource edit",
                "resourcexresource delete",
            ):
                continue

            for obj in serializers.deserialize("json", edit_log.oldvalue):
                obj.save()
                number_of_db_changes += 1

        return number_of_db_changes

    def _get_tile_data(self, tile: Tile) -> dict[str, Any]:
        return tile.data or {}

    def _get_preview_node_order(self, nodegroup: NodeGroup) -> list[str]:
        nodegroup_key = str(nodegroup.nodegroupid)
        if nodegroup_key in self._preview_node_order_cache:
            return self._preview_node_order_cache[nodegroup_key]

        nodes = Node.objects.filter(nodegroup=nodegroup)
        ordered_node_ids = [
            str(node.nodeid)
            for node in sorted(
                (node for node in nodes if node.sortorder is not None),
                key=lambda node: (node.sortorder, str(node.nodeid)),
            )
        ]
        self._preview_node_order_cache[nodegroup_key] = ordered_node_ids
        return ordered_node_ids

    def _get_ordered_tile_data_items(self, tile: Tile) -> list[tuple[str, Any]]:
        tile_data = self._get_tile_data(tile)
        ordered_items: list[tuple[str, Any]] = []

        tile_items = [(str(nodeid), value) for nodeid, value in tile_data.items()]
        if not tile_items:
            return ordered_items

        ordered_node_ids = self._get_preview_node_order(tile.nodegroup)
        tile_item_lookup = {nodeid: value for nodeid, value in tile_items}
        tile_node_ids = [nodeid for nodeid, _ in tile_items]

        preview_node_ids = [
            nodeid for nodeid in ordered_node_ids if nodeid in tile_item_lookup
        ]
        preview_node_ids.extend(
            nodeid for nodeid in tile_node_ids if nodeid not in preview_node_ids
        )

        for nodeid in preview_node_ids:
            ordered_items.append((nodeid, tile_item_lookup[nodeid]))

        return ordered_items

    def _get_nodegroup_preview_meta(self, node_group_id: str) -> dict[str, Any]:
        node_group_key = str(node_group_id)
        if node_group_key in self._preview_nodegroup_meta_cache:
            return self._preview_nodegroup_meta_cache[node_group_key]

        nodegroup = NodeGroup.objects.get(pk=node_group_key)
        nodegroup_node = self.get_node(nodegroup.nodegroupid)
        graph_ = models.GraphModel.objects.get(pk=nodegroup_node.graph_id)
        branch_name = "-" if graph_.isresource else graph_.name

        card = CardModel.objects.filter(nodegroup_id=nodegroup.nodegroupid).first()
        card_name = str(card.name) if card and card.name else None
        card_sortorder = card.sortorder if card else None

        nodes = list(Node.objects.filter(nodegroup=nodegroup))
        if not card_name:
            for node in nodes:
                card_x_node_widgets = CardXNodeXWidget.objects.filter(node=node)
                if not card_x_node_widgets.exists():
                    continue

                for widget in card_x_node_widgets:
                    widget_card: CardModel = widget.card
                    if widget_card and widget_card.name:
                        card_name = str(widget_card.name)
                        break
                if card_name:
                    break

        meta = {
            "nodegroup": nodegroup,
            "graph_id": str(graph_.graphid),
            "branch_name": branch_name,
            "card_name": card_name or node_group_key,
            "card_sortorder": card_sortorder,
        }
        self._preview_nodegroup_meta_cache[node_group_key] = meta
        return meta

    def _get_graph_card_display_order(self, graph_id: str) -> dict[str, int]:
        graph_key = str(graph_id)
        if graph_key in self._preview_graph_card_order_cache:
            return self._preview_graph_card_order_cache[graph_key]

        cards = list(
            CardModel.objects.filter(graph_id=graph_key).order_by("sortorder", "cardid")
        )
        nodegroups = {
            str(nodegroup.nodegroupid): nodegroup
            for nodegroup in NodeGroup.objects.filter(
                nodegroupid__in=[card.nodegroup_id for card in cards]
            )
        }
        cards_by_parent: dict[str | None, list[CardModel]] = {}

        for card in cards:
            nodegroup = nodegroups.get(str(card.nodegroup_id))
            parent_nodegroup_id = (
                str(nodegroup.parentnodegroup_id)
                if nodegroup and nodegroup.parentnodegroup_id
                else None
            )
            cards_by_parent.setdefault(parent_nodegroup_id, []).append(card)

        nodegroup_order: dict[str, int] = {}

        def walk(parent_nodegroup_id: str | None):
            for card in cards_by_parent.get(parent_nodegroup_id, []):
                nodegroup_id = str(card.nodegroup_id)
                if nodegroup_id not in nodegroup_order:
                    nodegroup_order[nodegroup_id] = len(nodegroup_order)
                walk(nodegroup_id)

        walk(None)

        self._preview_graph_card_order_cache[graph_key] = nodegroup_order
        return nodegroup_order

    def _get_nodegroup_preview_sort_key(self, node_group_id: str) -> tuple[Any, ...]:
        meta = self._get_nodegroup_preview_meta(node_group_id)
        nodegroup_order = self._get_graph_card_display_order(meta["graph_id"])
        display_index = nodegroup_order.get(str(node_group_id))
        card_sortorder = meta["card_sortorder"]
        return (
            display_index is None,
            display_index if display_index is not None else float("inf"),
            card_sortorder is None,
            card_sortorder if card_sortorder is not None else float("inf"),
            str(meta["card_name"]).lower(),
            str(node_group_id),
        )

    def _has_distinct_single_reference(
        self, base_tile: Tile, merge_tile: Tile, keys: list[str]
    ) -> bool:
        base_info = self._get_tile_data(base_tile)
        merge_info = self._get_tile_data(merge_tile)

        for key in keys:
            base_value = base_info.get(key)
            merge_value = merge_info.get(key)
            if not isinstance(base_value, list) or not isinstance(merge_value, list):
                continue

            base_ids = {
                item.get("resourceId")
                for item in base_value
                if isinstance(item, dict) and "resourceId" in item
            }
            merge_ids = {
                item.get("resourceId")
                for item in merge_value
                if isinstance(item, dict) and "resourceId" in item
            }

            if len(base_ids) == 1 and len(merge_ids) == 1 and base_ids != merge_ids:
                return True

        return False

    def check_if_values_contain_string_value(self, values: list[Tile], keys: list[str]):
        key_of_string_value: str | None = None

        for value in values:
            info = self._get_tile_data(value)

            for key in keys:
                if key_of_string_value:
                    break

                val = info.get(key)

                if val is None:
                    continue

                if isinstance(val, dict):
                    if "en" in val:
                        key_of_string_value = key
                        break

        return key_of_string_value

    def all_list_reference(self, keys: list[str], base: Tile) -> list[str]:
        listReference: list[str] = []

        info = self._get_tile_data(base)
        for key in keys:
            if info[key] is None:
                continue
            if isinstance(info[key], list):
                for infoTiledata in info[key]:
                    if "resourceId" in infoTiledata:
                        listReference.append(infoTiledata["resourceId"])
        return listReference

    def check_for_same_information_preview(
        self,
        value: Tile,
        matching_tiles: list[Tile],
        keys: list[str],
        source_references_id: str,
        flag_label: bool = False,
        order: int | None = None,
        baseId: str | None = None,
    ):

        info = self._get_tile_data(value)
        important_value = None
        added_resources = []
        removed_resources = []
        added_reference_tileid = []
        merged_reference_tileid = []

        # extract datatype from tiles table
        with connection.cursor() as cursor:
            cursor.execute(
                """select datatype
                   from tiles join nodes on nodes.nodeid=tiles.nodegroupid
                   where tileid= %s;
                   """,
                (value.tileid,),
            )

            rows = cursor.fetchall()
        data_type = rows[0] if rows else None

        if len(matching_tiles) == 1 and not flag_label:
            base = matching_tiles[0]
            mergeBaseResource = self._get_tile_data(base)
            listReference = self.all_list_reference(keys, base)
            should_delete = True
            for key in keys:
                if info[key] is None:
                    continue
                if isinstance(info[key], list):
                    for infoTiledata in info[key]:
                        if "resourceId" in infoTiledata:
                            if infoTiledata["resourceId"] not in listReference:
                                should_delete = False
                                if baseId is None:
                                    return (
                                        added_resources,
                                        removed_resources,
                                        added_reference_tileid,
                                        merged_reference_tileid,
                                    )
                                mergeBaseResource[key].append(infoTiledata)
                                if key == source_references_id:
                                    added_reference_tileid.append(
                                        infoTiledata["resourceId"]
                                    )
                                added_resources.append(str(value.tileid))
                            else:
                                if key == source_references_id:
                                    merged_reference_tileid.append(
                                        infoTiledata["resourceId"]
                                    )
                        else:
                            if baseId is None:
                                return (
                                    added_resources,
                                    removed_resources,
                                    added_reference_tileid,
                                    merged_reference_tileid,
                                )

                            if infoTiledata not in mergeBaseResource[key]:
                                should_delete = False

                                added_resources.append(str(value.tileid))
            if baseId is not None:
                if should_delete:
                    tiles = Tile.objects.filter(tileid=base.tileid)
                    for tile in tiles:
                        tile.resourceinstance = Resource.objects.get(pk=baseId)

                        added_resources.append(str(tile.tileid))
                else:
                    for tile in Tile.objects.filter(tileid=value.tileid):
                        removed_resources.append(tile.tileid)

                    for tile in Tile.objects.filter(tileid=base.tileid):
                        tile.data = mergeBaseResource

                        added_resources.append(str(tile.tileid))
        else:
            parent_tile_exists = Tile.objects.filter(tileid=value.tileid).exists()
            tiledata_value = self._get_tile_data(value)
            if parent_tile_exists and flag_label:
                flag_update = False
                for i, info in enumerate(matching_tiles):
                    tiledata_merge = self._get_tile_data(info)

                    for key in keys:
                        if not tiledata_merge.get(key) or not isinstance(
                            tiledata_value[key], list
                        ):
                            continue

                        for tile_data in tiledata_merge[key]:
                            if (
                                "resourceId" not in tile_data
                                or tiledata_value[key] is None
                            ):
                                continue
                            for help_tiledata in tiledata_value[key]:
                                if (
                                    tile_data["resourceId"]
                                    == help_tiledata["resourceId"]
                                ):
                                    flag_update = True
                                    index_value = i
                                    if key == source_references_id:
                                        merged_reference_tileid.append(
                                            tile_data["resourceId"]
                                        )
                                        if (
                                            tile_data["resourceId"]
                                            in added_reference_tileid
                                        ):
                                            added_reference_tileid.remove(
                                                tile_data["resourceId"]
                                            )

                                else:
                                    if key == source_references_id:
                                        added_reference_tileid.append(
                                            help_tiledata["resourceId"]
                                        )

                if data_type and data_type[0] == "concept-list":
                    for i, info in enumerate(matching_tiles):  # base
                        tiledata_merge = self._get_tile_data(info)
                        for key in keys:
                            new_value = set()
                            if tiledata_merge.get(key):
                                if not isinstance(tiledata_merge[key], list):
                                    new_value.add(tiledata_merge[key])
                                else:
                                    for tiledata in tiledata_merge[key]:
                                        new_value.add(tiledata)
                            if tiledata_value.get(key):
                                if not isinstance(tiledata_value[key], list):
                                    new_value.add(tiledata_value[key])
                                    added_resources.append(str(value.tileid))
                                else:
                                    for tiledata in tiledata_value[key]:  # merge
                                        new_value.add(tiledata)
                                    added_resources.append(str(value.tileid))
                            new_value = list(new_value)
                            if (
                                not isinstance(tiledata_merge[key], list)
                                and not isinstance(tiledata_value[key], list)
                                and len(new_value) == 1
                            ):
                                tiledata_merge[key] = new_value[0]
                            elif (
                                not isinstance(tiledata_merge[key], list)
                                and not isinstance(tiledata_value[key], list)
                                and len(new_value) == 0
                            ):
                                tiledata_merge[key] = tiledata_merge[key]
                            else:
                                tiledata_merge[key] = new_value

                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[i].tileid
                        ):
                            tile.data = tiledata_merge
                            added_resources.append(str(tile.tileid))

                else:
                    if not flag_update:
                        for tile in Tile.objects.filter(tileid=value.tileid):
                            tile.resourceinstance = Resource.objects.get(pk=baseId)

                            added_resources.append(str(tile.tileid))
                    else:
                        tiledata_merge = self._get_tile_data(
                            matching_tiles[index_value]
                        )
                        for key in keys:
                            list_info = []
                            if tiledata_merge[key] is None:
                                continue
                            if isinstance(tiledata_merge[key], list):
                                for info_tiledata in tiledata_merge[key]:
                                    if "resourceId" in info_tiledata:
                                        list_info.append(info_tiledata["resourceId"])
                                for info_tiledata in tiledata_value[key]:
                                    if "resourceId" in info_tiledata:
                                        if info_tiledata["resourceId"] not in list_info:
                                            tiledata_merge[key].append(info_tiledata)

                                            if key == source_references_id:
                                                added_reference_tileid.append(
                                                    info_tiledata["resourceId"]
                                                )
                                            added_resources.append(value.tileid)
                                        else:
                                            if key == source_references_id:
                                                merged_reference_tileid.append(
                                                    info_tiledata["resourceId"]
                                                )
                                                if (
                                                    info_tiledata["resourceId"]
                                                    in added_reference_tileid
                                                ):
                                                    added_reference_tileid.remove(
                                                        info_tiledata["resourceId"]
                                                    )

                                    elif info_tiledata not in tiledata_merge[key]:
                                        tiledata_merge[key].append(info_tiledata)
                                        if key == source_references_id:
                                            for infoTiledata in info_tiledata:
                                                added_reference_tileid.append(
                                                    infoTiledata["resourceId"]
                                                )
                                        added_resources.append(value.tileid)
                            elif (
                                tiledata_merge[key] is None
                                and tiledata_value[key] is not None
                            ):
                                tiledata_merge[key] = tiledata_value[key]
                                if key == source_references_id:
                                    for infoTiledata in info_tiledata:
                                        added_reference_tileid.append(
                                            infoTiledata["resourceId"]
                                        )
                                added_resources.append(str(value.tileid))

                        for tile in Tile.objects.filter(tileid=value.tileid):
                            tile.parenttile = matching_tiles[index_value]
                            added_resources.append(str(tile.tileid))

                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[index_value].tileid
                        ):
                            tile.data = tiledata_merge
                            added_resources.append(str(tile.tileid))

            else:
                flag = False
                listReference: list[str] = []
                for i, infoTiledata in enumerate(matching_tiles):
                    if not self._get_tile_data(infoTiledata):
                        if baseId is None:
                            (
                                added_resources,
                                removed_resources,
                                added_reference_tileid,
                                merged_reference_tileid,
                            )
                        else:
                            flag = True
                    else:
                        listReference.extend(
                            self.all_list_reference(keys, infoTiledata)
                        )
                        important_value = i
                        if listReference == []:
                            if baseId is None:
                                (
                                    added_resources,
                                    removed_resources,
                                    added_reference_tileid,
                                    merged_reference_tileid,
                                )
                        else:
                            if baseId is None:
                                (
                                    added_resources,
                                    removed_resources,
                                    added_reference_tileid,
                                    merged_reference_tileid,
                                )
                if info and important_value is not None:
                    mergesTiledata = self._get_tile_data(
                        matching_tiles[important_value]
                    )
                    for resource in info[keys[0]]:
                        if flag:
                            if "resourceId" not in resource:
                                continue

                            if resource["resourceId"] not in listReference:
                                if baseId is None:
                                    (
                                        added_resources,
                                        removed_resources,
                                        added_reference_tileid,
                                        merged_reference_tileid,
                                    )

                                mergesTiledata[keys[0]].append(resource)
                                added_resources.append(value.tileid)

                        else:
                            if "resourceId" not in resource:
                                continue
                            if resource["resourceId"] not in listReference:
                                if baseId is None:
                                    (
                                        added_resources,
                                        removed_resources,
                                        added_reference_tileid,
                                        merged_reference_tileid,
                                    )
                                for tile in Tile.objects.filter(tileid=value.tileid):
                                    tile.sortorder = order
                                    tile.resourceinstance = Resource.objects.get(
                                        pk=baseId
                                    )

                    if flag:
                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[important_value].tileid
                        ):
                            tile.data = mergesTiledata
                            added_resources.append(tile.tileid)

        if baseId is None:
            return (
                added_resources,
                removed_resources,
                added_reference_tileid,
                merged_reference_tileid,
            )
        return (
            added_resources,
            removed_resources,
            added_reference_tileid,
            merged_reference_tileid,
        )

    def check_for_same_information(
        self,
        value: Tile,
        matching_tiles: list[Tile],
        keys: list[str],
        flag_label: bool = False,
        order: int | None = None,
        baseId: str | None = None,
    ):

        info = self._get_tile_data(value)
        important_value = None

        # extract datatype from tiles table
        with connection.cursor() as cursor:
            cursor.execute(
                """select datatype
                   from tiles join nodes on nodes.nodeid=tiles.nodegroupid
                   where tileid= %s;
                   """,
                (value.tileid,),
            )

            rows = cursor.fetchall()
        data_type = rows[0] if rows else None

        if len(matching_tiles) == 1 and not flag_label:
            base = matching_tiles[0]
            mergeBaseResource = self._get_tile_data(base)
            listReference = self.all_list_reference(keys, base)
            should_delete = True
            for key in keys:
                if info[key] is None:
                    continue
                if isinstance(info[key], list):
                    for infoTiledata in info[key]:
                        if "resourceId" in infoTiledata:
                            if infoTiledata["resourceId"] not in listReference:
                                should_delete = False
                                if baseId is None:
                                    return True

                                for (
                                    resource_x_resource
                                ) in ResourceXResource.objects.filter(
                                    resourcexid=infoTiledata["resourceXresourceId"]
                                ):
                                    setattr(resource_x_resource, RXR_FROM_FIELD, "")
                                    setattr(resource_x_resource, RXR_TILE_FIELD, base)
                                    self._resource_x_resource_save(resource_x_resource)

                                mergeBaseResource[key].append(infoTiledata)
                        else:
                            if baseId is None:
                                return True

                            if infoTiledata not in mergeBaseResource[key]:
                                should_delete = False
                                mergeBaseResource[key].append(infoTiledata)
            if baseId is not None:
                if should_delete:
                    tiles = Tile.objects.filter(tileid=base.tileid)
                    for tile in tiles:
                        tile.resourceinstance = Resource.objects.get(pk=baseId)
                        self._tile_save(tile)
                else:
                    for rxr in ResourceXResource.objects.filter(
                        **{RXR_TILE_FIELD: value.tileid}
                    ):
                        self._resource_x_resource_delete(rxr)

                    for tile in Tile.objects.filter(tileid=value.tileid):
                        self._tile_delete(tile)

                    for tile in Tile.objects.filter(tileid=base.tileid):
                        tile.data = mergeBaseResource
                        self._tile_save(tile)
        else:
            parent_tile_exists = Tile.objects.filter(tileid=value.tileid).exists()
            tiledata_value = self._get_tile_data(value)
            if parent_tile_exists and flag_label:
                flag_update = False
                for i, info in enumerate(matching_tiles):
                    tiledata_merge = self._get_tile_data(info)
                    for key in keys:
                        if not tiledata_merge.get(key) or not isinstance(
                            tiledata_value[key], list
                        ):
                            continue

                        for tile_data in tiledata_merge[key]:
                            if (
                                "resourceId" not in tile_data
                                or tiledata_value[key] is None
                            ):
                                continue
                            for help_tiledata in tiledata_value[key]:
                                if (
                                    tile_data["resourceId"]
                                    == help_tiledata["resourceId"]
                                ):
                                    flag_update = True
                                    index_value = i

                if data_type and data_type[0] == "concept-list":
                    for i, info in enumerate(matching_tiles):  # base
                        tiledata_merge = self._get_tile_data(info)
                        for key in keys:
                            new_value = set()
                            if tiledata_merge.get(key):
                                if not isinstance(tiledata_merge[key], list):
                                    new_value.add(tiledata_merge[key])
                                else:
                                    for tiledata in tiledata_merge[key]:
                                        new_value.add(tiledata)
                            if tiledata_value.get(key):
                                if not isinstance(tiledata_value[key], list):
                                    new_value.add(tiledata_value[key])
                                else:
                                    for tiledata in tiledata_value[key]:  # merge
                                        new_value.add(tiledata)
                            new_value = list(new_value)
                            if (
                                not isinstance(tiledata_merge[key], list)
                                and not isinstance(tiledata_value[key], list)
                                and len(new_value) == 1
                            ):
                                tiledata_merge[key] = new_value[0]
                            elif (
                                not isinstance(tiledata_merge[key], list)
                                and not isinstance(tiledata_value[key], list)
                                and len(new_value) == 0
                            ):
                                tiledata_merge[key] = tiledata_merge[key]
                            else:
                                tiledata_merge[key] = new_value

                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[i].tileid
                        ):
                            tile.data = tiledata_merge
                            self._tile_save(tile)

                else:
                    if not flag_update:
                        for rxr in ResourceXResource.objects.filter(
                            **{RXR_TILE_FIELD: value.tileid}
                        ):
                            setattr(
                                rxr,
                                RXR_FROM_FIELD,
                                Resource.objects.get(pk=baseId),
                            )
                            self._resource_x_resource_save(rxr)

                        for tile in Tile.objects.filter(tileid=value.tileid):
                            tile.resourceinstance = Resource.objects.get(pk=baseId)
                            self._tile_save(tile)
                    else:
                        tiledata_merge = self._get_tile_data(
                            matching_tiles[index_value]
                        )
                        for key in keys:
                            list_info = []
                            if tiledata_merge[key] is None:
                                continue
                            if isinstance(tiledata_merge[key], list):
                                for info_tiledata in tiledata_merge[key]:
                                    if "resourceId" in info_tiledata:
                                        list_info.append(info_tiledata["resourceId"])
                                for info_tiledata in tiledata_value[key]:
                                    if "resourceId" in info_tiledata:
                                        if info_tiledata["resourceId"] not in list_info:
                                            tiledata_merge[key].append(info_tiledata)
                                            updatevalue = """UPDATE resource_x_resource set resourceinstanceidfrom=%s,tileid=%s  where tileid=%s ; """
                                            with connection.cursor() as cursor:
                                                cursor.execute(
                                                    updatevalue,
                                                    (
                                                        baseId,
                                                        matching_tiles[
                                                            index_value
                                                        ].tileid,
                                                        value.tileid,
                                                    ),
                                                )
                                    elif info_tiledata not in tiledata_merge[key]:
                                        tiledata_merge[key].append(info_tiledata)
                            elif (
                                tiledata_merge[key] is None
                                and tiledata_value[key] is not None
                            ):
                                tiledata_merge[key] = tiledata_value[key]
                        for rxr in ResourceXResource.objects.filter(
                            **{RXR_TILE_FIELD: value.tileid}
                        ):
                            self._resource_x_resource_delete(rxr)

                        for tile in Tile.objects.filter(tileid=value.tileid):
                            tile.parenttile = matching_tiles[index_value]
                            self._tile_save(tile)

                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[index_value].tileid
                        ):
                            tile.data = tiledata_merge
                            self._tile_save(tile)

            else:
                flag = False

                listReference: list[str] = []
                for i, infoTiledata in enumerate(matching_tiles):
                    if not self._get_tile_data(infoTiledata):
                        if baseId is None:
                            return False
                        else:
                            flag = True
                    else:
                        listReference.extend(
                            self.all_list_reference(keys, infoTiledata)
                        )
                        important_value = i
                        if listReference == []:
                            if baseId is None:
                                return False
                        else:
                            if baseId is None:
                                return True

                if info and important_value is not None:
                    mergesTiledata = self._get_tile_data(
                        matching_tiles[important_value]
                    )
                    for resource in info[keys[0]]:
                        if flag:
                            if "resourceId" not in resource:
                                continue

                            if resource["resourceId"] not in listReference:
                                if baseId is None:
                                    return True

                                for rxr in ResourceXResource.objects.filter(
                                    resourcexid=resource["resourceXresourceId"]
                                ):
                                    setattr(
                                        rxr,
                                        RXR_FROM_FIELD,
                                        Resource.objects.get(pk=baseId),
                                    )
                                    setattr(
                                        rxr,
                                        RXR_TILE_FIELD,
                                        matching_tiles[important_value],
                                    )
                                    self._resource_x_resource_save(rxr)

                                mergesTiledata[keys[0]].append(resource)

                        else:
                            if "resourceId" not in resource:
                                continue
                            if resource["resourceId"] not in listReference:
                                if baseId is None:
                                    return True
                                for tile in Tile.objects.filter(tileid=value.tileid):
                                    tile.sortorder = order
                                    tile.resourceinstance = Resource.objects.get(
                                        pk=baseId
                                    )

                                for rxr in ResourceXResource.objects.filter(
                                    resourcexid=resource["resourceXresourceId"]
                                ):
                                    setattr(
                                        rxr,
                                        RXR_FROM_FIELD,
                                        Resource.objects.get(pk=baseId),
                                    )
                                    setattr(
                                        rxr,
                                        RXR_TILE_FIELD,
                                        matching_tiles[important_value],
                                    )
                                    self._resource_x_resource_save(rxr)

                    if flag:
                        for tile in Tile.objects.filter(
                            tileid=matching_tiles[important_value].tileid
                        ):
                            tile.data = mergesTiledata
                            self._tile_save(tile)
        if baseId is None:
            return False

    def get_display_values_source_reference(
        self, tile: Tile, node: Node, datatype, added_reference_tileid=None
    ):
        data = datatype.get_tile_data(tile)
        if data:
            nodevalue = datatype.get_nodevalues(data[str(node.nodeid)])

            items = []
            for resourceXresource in nodevalue:
                try:
                    resourceid = resourceXresource["resourceId"]
                    related_resource = Resource.objects.get(pk=resourceid)
                    displayname = related_resource.displayname()
                    if displayname is None:
                        displayname = "Missing display name"
                    if added_reference_tileid is not None:
                        if resourceid in added_reference_tileid:
                            items.append((displayname, "added"))
                        else:
                            items.append((displayname, "merged"))
                    else:
                        items.append((displayname, resourceid, "unknown"))
                except Resource.DoesNotExist:
                    continue
            return items

    def _get_widget_label(self, node, nod):
        widget_label = ""
        card_x_node_widgets = CardXNodeXWidget.objects.filter(node=node)
        if card_x_node_widgets.exists():
            for widget in card_x_node_widgets:
                if widget.config:
                    widget_label = widget.config.get("label", "")

        if len(widget_label) == 0:
            widget_label = nod.alias
        return widget_label

    def get_mergeable_nodegroups(self, request: HttpRequest):
        base_resource: str | None = request.POST.get("resourceBase", None)
        merge_resources: list[str] = request.POST.get("mergeResources", "").split(",")
        user_name = request.user.username

        resource = Resource.objects.get(pk=base_resource)
        graph = Graph.objects.get(graphid=resource.graph_id)

        source_references_id = (
            str(self.get_source_references_ids(graph.graphid)[0])
            if self.get_source_references_ids(graph.graphid)
            else None
        )

        if not base_resource:
            raise MissingRequiredInputError(
                _("The 'resourceBase' parameter is required.")
            )

        base_resource_graph = Resource.objects.get(pk=base_resource).graph
        base_resource_tiles = list(Tile.objects.filter(resourceinstance=base_resource))

        if not base_resource_tiles:
            return {
                "success": True,
                "data": {
                    "info": "No",
                    "info_message": f"The resource with ID {base_resource} does not exist",
                },
            }
        (
            _,
            _,
            added_tiles,
            merged_tiles,
            dict_counts,
            added_reference_tileid,
            merged_reference_tileid,
        ) = self.calculate_which_added_which_removed(
            base_resource, merge_resources, source_references_id
        )
        node_group_ids: set[str] = set()
        tiles_per_nodegroup: dict[str, set[Tile]] = {}
        for merge_resource in merge_resources:
            merge_resource_graph = Resource.objects.get(pk=merge_resource).graph

            if merge_resource_graph != base_resource_graph:
                return {
                    "success": True,
                    "data": {
                        "info": "No",
                        "info_message": f"The resources with IDs {merge_resource} and {base_resource} are from different graphs",
                    },
                }
            merge_resource_tiles = list(
                Tile.objects.filter(resourceinstance=merge_resource)
            )
            if not merge_resource_tiles:
                return {
                    "success": True,
                    "data": {
                        "info": "No",
                        "info_message": f"The resource with ID {merge_resource} does not exist",
                    },
                }
            for merge_tile in merge_resource_tiles:
                if str(merge_tile.nodegroup.nodegroupid) not in tiles_per_nodegroup:
                    tiles_per_nodegroup[str(merge_tile.nodegroup.nodegroupid)] = set()

                node_group_ids.add(str(merge_tile.nodegroup.nodegroupid))
                tiles_per_nodegroup[str(merge_tile.nodegroup.nodegroupid)].add(
                    merge_tile
                )

        node_groups_to_return: set[str] = set(node_group_ids).union(
            set(tiles_per_nodegroup.keys())
        )
        node_group_details: list[dict[str, object]] = []
        for node_group_id in sorted(
            node_groups_to_return, key=self._get_nodegroup_preview_sort_key
        ):
            meta = self._get_nodegroup_preview_meta(node_group_id)
            nodegroup = meta["nodegroup"]
            branch_name = meta["branch_name"]
            card_name = meta["card_name"]

            tile_details: list[dict[str, str]] = []
            tile_set = tiles_per_nodegroup.get(str(node_group_id), set())
            for tile in sorted(tile_set, key=lambda t: str(t.tileid)):
                tiledata = []

                for nodeid, _ in self._get_ordered_tile_data_items(tile):
                    node = self.get_node(nodeid)
                    nod = models.Node.objects.get(pk=nodeid)
                    widget_label = self._get_widget_label(node, nod)
                    datatype = self.datatype_factory.get_instance(node.datatype)
                    if nodeid == source_references_id:
                        values = self.get_display_values_source_reference(
                            tile, node, datatype, added_reference_tileid
                        )
                        for value in values:
                            tiledata.append((widget_label, value[0], value[1]))
                    else:
                        tiledata.append(
                            (widget_label, datatype.get_display_value(tile, node))
                        )

                if str(tile.tileid) in added_tiles:
                    status = "added"
                elif str(tile.tileid) in merged_tiles:
                    status = "merged"
                else:
                    status = "Unknown"
                tile_details.append(
                    {
                        "tileId": str(tile.tileid),
                        "parenttileid": str(tile.parenttile.tileid)
                        if tile.parenttile
                        else None,
                        "resourceId": str(tile.resourceinstance.resourceinstanceid),
                        "tiledata": tiledata,
                        "status": status,
                    }
                )

            node_group_details.append(
                {
                    "nodegroupId": str(node_group_id),
                    "name": card_name,
                    "branch_name": branch_name,
                    "tiles": tile_details,
                }
            )
        return {
            "success": True,
            "data": {
                "info": "Yes",
                "data": node_group_details,
                "dict_counts": dict_counts,
                "user_name": user_name,
            },
        }

    def recalculate_dict(self, request: HttpRequest):
        base_resource: str | None = request.POST.get("resourceBase", None)
        merge_resources: list[str] = request.POST.get("mergeResources", "").split(",")
        tiles_excluded = request.POST.get("excluded_tile_ids", "")

        resource = Resource.objects.get(pk=base_resource)
        graph = Graph.objects.get(graphid=resource.graph_id)

        source_references_id = (
            str(self.get_source_references_ids(graph.graphid)[0])
            if self.get_source_references_ids(graph.graphid)
            else "0"
        )

        if not base_resource:
            raise MissingRequiredInputError(
                _("The 'resourceBase' parameter is required.")
            )

        base_resource_tiles = list(Tile.objects.filter(resourceinstance=base_resource))

        if not base_resource_tiles:
            return {
                "success": True,
                "data": {
                    "info": "No",
                    "info_message": f"The resource with ID {base_resource} does not exist",
                },
            }
        _, _, _, _, dict_counts, _, _ = self.calculate_which_added_which_removed(
            base_resource, merge_resources, source_references_id, tiles_excluded
        )

        return {
            "success": True,
            "data": {
                "info": "Yes",
                "dict_counts": dict_counts,
            },
        }

    def get_name_card(self, tile: Tile):
        nodegroupp = models.NodeGroup.objects.get(pk=tile.nodegroup.nodegroupid)
        nodes = Node.objects.filter(nodegroup=nodegroupp)
        for node in nodes:
            card_x_node_widgets = CardXNodeXWidget.objects.filter(node=node)
            if not card_x_node_widgets.exists():
                continue

            for widget in card_x_node_widgets:
                card: CardModel = widget.card
                if card:
                    card_name = str(card.name)
                    break
            if card_name:
                break
        card_name = card_name or str(tile.nodegroup.nodegroupid)
        return card_name, str(tile.nodegroup.nodegroupid)

    def get_card_name_by_nodegroupid(self, nodegroupid):
        nodegroupp = models.NodeGroup.objects.get(pk=nodegroupid)
        nodes = Node.objects.filter(nodegroup=nodegroupp)
        card_name = None
        for node in nodes:
            card_x_node_widgets = CardXNodeXWidget.objects.filter(node=node)
            if not card_x_node_widgets.exists():
                continue

            for widget in card_x_node_widgets:
                card: CardModel = widget.card
                if card:
                    card_name = str(card.name)
                    break
            if card_name:
                break
        return card_name or str(nodegroupid)

    def calculate_which_added_which_removed(
        self,
        base_resource: str,
        merge_resources: list[str],
        source_references_id=str,
        excluded_tile_ids=None,
    ):

        base_resource_tiles = list(Tile.objects.filter(resourceinstance=base_resource))
        dict_counts = {}
        if excluded_tile_ids is None:
            excluded_tile_ids = []
        unique_reference_resources = set()
        for tile in base_resource_tiles:
            card_name, node_group_id = self.get_name_card(tile)
            if card_name not in dict_counts:
                dict_counts[card_name] = {}
                if node_group_id == source_references_id:
                    for tile_data in tile.data.get(source_references_id, []):
                        unique_reference_resources.add(tile_data.get("resourceId"))
                else:
                    dict_counts[card_name][str(base_resource) + "_pre"] = 1
            else:
                if node_group_id == source_references_id:
                    for tile_data in tile.data.get(source_references_id, []):
                        unique_reference_resources.add(tile_data.get("resourceId"))
                else:
                    dict_counts[card_name][str(base_resource) + "_pre"] += 1
        if source_references_id:
            reference_card_name = self.get_card_name_by_nodegroupid(source_references_id)
            if len(unique_reference_resources) > 0:
                dict_counts[reference_card_name][str(base_resource) + "_pre"] = len(
                    unique_reference_resources
                )
        added_tile = {}
        removed_tile = {}
        added_tiles = []
        merged_tiles = []
        added_reference_tileid = []
        merged_reference_tileid = []

        for merge_resource in merge_resources:
            merge_resource_tiles = list(
                Tile.objects.filter(resourceinstance=merge_resource)
            )

            added, removed, resource_id_added_, resource_id_merged_ = (
                self._process_merge_resource_preview(
                    merge_resource,
                    base_resource,
                    base_resource_tiles,
                    source_references_id,
                    excluded_tile_ids,
                )
            )
            merged_merge_resource_tiles_ids = [
                str(tile.tileid)
                for tile in merge_resource_tiles
                if str(tile.tileid) not in added
                and str(tile.tileid) not in excluded_tile_ids
            ]
            added_merge_resource_tiles_ids = [
                str(tile.tileid)
                for tile in merge_resource_tiles
                if str(tile.tileid) in added
            ]
            resource_id_added_ = list(set(resource_id_added_))

            resource_id_merged_ = list(set(resource_id_merged_))
            resource_id_added_ = [
                res for res in resource_id_added_ if res not in resource_id_merged_
            ]
            added_reference_tileid.extend(resource_id_added_)
            merged_reference_tileid.extend(resource_id_merged_)

            # clean the added and merged tiles for merge  for the cases of source referecnces
            for tile in merge_resource_tiles:
                if (
                    str(tile.tileid) in added
                    and str(tile.tileid) not in excluded_tile_ids
                ):
                    if len(tile.data.get(source_references_id, [])) > 0:
                        count_added = 0
                        for tile_data in tile.data.get(source_references_id, []):
                            if tile_data.get("resourceId") in added_reference_tileid:
                                count_added += 1
                        if count_added == 0:
                            added_merge_resource_tiles_ids.remove(str(tile.tileid))
                            merged_merge_resource_tiles_ids.append(str(tile.tileid))

            added_tile[merge_resource] = {
                "added": added_merge_resource_tiles_ids,
                "merged": merged_merge_resource_tiles_ids,
            }
            added_tiles.extend(added_merge_resource_tiles_ids)
            merged_tiles.extend(merged_merge_resource_tiles_ids)

            for tile in merge_resource_tiles:
                card_name, node_group_id = self.get_name_card(tile)
                if card_name not in dict_counts:
                    dict_counts[card_name] = {}
                    if str(tile.tileid) in added:
                        if node_group_id == source_references_id:
                            dict_counts[card_name][str(merge_resource) + "_added"] = (
                                len(set(resource_id_added_))
                            )
                            dict_counts[card_name][str(merge_resource) + "_merged"] = (
                                len(set(resource_id_merged_))
                            )
                        else:
                            dict_counts[card_name][str(merge_resource) + "_added"] = 1
                    if str(tile.tileid) in merged_merge_resource_tiles_ids:
                        if node_group_id == source_references_id:
                            dict_counts[card_name][str(merge_resource) + "_merged"] = (
                                len(set(resource_id_merged_))
                            )
                        else:
                            dict_counts[card_name][str(merge_resource) + "_merged"] = 1
                    if str(tile.tileid) in excluded_tile_ids:
                        if node_group_id == source_references_id:
                            num, _ = self._count_source_references_tile(
                                str(tile.tileid)
                            )
                            dict_counts[card_name][str(merge_resource) + "_exluded"] = (
                                num
                            )
                        else:
                            dict_counts[card_name][str(merge_resource) + "_exluded"] = 1

                elif (
                    (card_name in dict_counts)
                    and (
                        str(merge_resource) + "_added"
                        not in dict_counts[card_name].keys()
                    )
                    and (str(tile.tileid) in added)
                ):
                    if node_group_id == source_references_id:
                        dict_counts[card_name][str(merge_resource) + "_added"] = len(
                            set(resource_id_added_)
                        )
                        dict_counts[card_name][str(merge_resource) + "_merged"] = len(
                            set(resource_id_merged_)
                        )
                    else:
                        dict_counts[card_name][str(merge_resource) + "_added"] = 1
                elif (
                    card_name in dict_counts
                    and str(merge_resource) + "_merged"
                    not in dict_counts[card_name].keys()
                ) and (str(tile.tileid) in merged_merge_resource_tiles_ids):
                    if node_group_id == source_references_id:
                        dict_counts[card_name][str(merge_resource) + "_merged"] = len(
                            set(resource_id_merged_)
                        )
                    else:
                        dict_counts[card_name][str(merge_resource) + "_merged"] = 1
                elif (
                    card_name in dict_counts
                    and str(merge_resource) + "_exluded"
                    not in dict_counts[card_name].keys()
                ) and (str(tile.tileid) in excluded_tile_ids):
                    if node_group_id == source_references_id:
                        num, _ = self._count_source_references_tile(str(tile.tileid))
                        dict_counts[card_name][str(merge_resource) + "_exluded"] = num
                    else:
                        dict_counts[card_name][str(merge_resource) + "_exluded"] = 1
                else:
                    if str(tile.tileid) in added:
                        if node_group_id == source_references_id:
                            dict_counts[card_name][str(merge_resource) + "_added"] = (
                                len(set(resource_id_added_))
                            )
                            dict_counts[card_name][str(merge_resource) + "_merged"] = (
                                len(set(resource_id_merged_))
                            )
                        else:
                            dict_counts[card_name][str(merge_resource) + "_added"] += 1
                    if str(tile.tileid) in merged_merge_resource_tiles_ids:
                        if node_group_id == source_references_id:
                            dict_counts[card_name][str(merge_resource) + "_merged"] = (
                                len(set(resource_id_merged_))
                            )
                        else:
                            dict_counts[card_name][str(merge_resource) + "_merged"] += 1
                    if str(tile.tileid) in excluded_tile_ids:
                        if node_group_id == source_references_id:
                            num, _ = self._count_source_references_tile(
                                str(tile.tileid)
                            )
                            dict_counts[card_name][
                                str(merge_resource) + "_exluded"
                            ] += num
                        else:
                            dict_counts[card_name][
                                str(merge_resource) + "_exluded"
                            ] += 1

        expected_keys = [f"{base_resource}_pre"]
        for mr in merge_resources:
            expected_keys.append(f"{mr}_added")
            expected_keys.append(f"{mr}_merged")
        expected_keys.append(f"{base_resource}_post_merge")

        # Ensure every card has all expected keys (fill missing with 0)
        for card_name in dict_counts:
            for k in expected_keys:
                dict_counts[card_name].setdefault(k, 0)

        for card_name, counts in dict_counts.items():
            pre_count = counts.get(str(base_resource) + "_pre", 0)
            added_count = sum(
                count for key, count in counts.items() if key.endswith("_added")
            )
            dict_counts[card_name][f"{base_resource}_post_merge"] = (
                added_count + pre_count
            )

        return (
            added_tile,
            removed_tile,
            added_tiles,
            merged_tiles,
            dict_counts,
            added_reference_tileid,
            merged_reference_tileid,
        )

    def get_node(self, nodeid):
        nodeid = str(nodeid)
        try:
            return self.node_lookup[nodeid]
        except KeyError:
            self.node_lookup[nodeid] = models.Node.objects.get(pk=nodeid)
            return self.node_lookup[nodeid]

    def _normalize_tile_match_value(self, value: Any) -> tuple[str, Any]:
        if isinstance(value, list):
            resource_ids: list[str] = []
            other_items: list[str] = []
            for item in value:
                if isinstance(item, dict) and "resourceId" in item:
                    resource_ids.append(str(item["resourceId"]))
                else:
                    other_items.append(
                        json.dumps(item, sort_keys=True, ensure_ascii=False)
                    )
            return (
                "list",
                (
                    tuple(sorted(resource_ids)),
                    tuple(sorted(other_items)),
                ),
            )

        if isinstance(value, dict):
            return ("dict", json.dumps(value, sort_keys=True, ensure_ascii=False))

        return ("scalar", value)

    def _tile_matches_on_keys(
        self, left_tile: Tile, right_tile: Tile, keys: list[str] | None
    ) -> bool:
        if not keys:
            return self._get_tile_data(left_tile) == self._get_tile_data(right_tile)

        left_info = self._get_tile_data(left_tile)
        right_info = self._get_tile_data(right_tile)
        for key in keys:
            if self._normalize_tile_match_value(
                left_info.get(key)
            ) != self._normalize_tile_match_value(right_info.get(key)):
                return False

        return True

    def _find_matching_tile(
        self, tile: Tile, candidates: list[Tile], keys: list[str] | None
    ) -> Tile | None:
        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0]

        matches = [
            candidate
            for candidate in candidates
            if self._tile_matches_on_keys(tile, candidate, keys)
        ]
        if len(matches) == 1:
            return matches[0]

        return None

    def update_value(
        self,
        base: str,
        value: Tile,
        order: int,
        keys: list[str] | None,
        parenttile: Tile | None = None,
    ):
        value.resourceinstance = Resource.objects.get(pk=base)
        value.sortorder = order
        if parenttile is not None:
            value.parenttile = parenttile
        self._tile_save(value)

        if not keys:
            return
        nameInfo = self._get_tile_data(value)
        for key in keys:
            if nameInfo[key] is None:
                continue
            if isinstance(nameInfo[key], list):
                for info in nameInfo[key]:
                    if "resourceId" not in info:
                        continue

                    for rxr in ResourceXResource.objects.filter(
                        resourcexid=info["resourceXresourceId"]
                    ):
                        setattr(
                            rxr,
                            RXR_FROM_FIELD,
                            Resource.objects.get(pk=base),
                        )
                        self._resource_x_resource_save(rxr)

    def merge_reference(
        self, baseId: str, valueMerge: Tile, base: Tile, keys: list[str] | None
    ):
        if not keys:
            return

        listReference = self.all_list_reference(keys, base)
        valueInfo = self._get_tile_data(valueMerge)
        baseInfo = self._get_tile_data(base)

        for key in keys:
            if valueInfo[key] is None:
                continue
            if isinstance(valueInfo[key], list):
                for info in valueInfo[key]:
                    if "resourceId" in info:
                        if info["resourceId"] not in listReference:
                            for rxr in ResourceXResource.objects.filter(
                                resourcexid=info["resourceXresourceId"]
                            ):
                                setattr(
                                    rxr,
                                    RXR_FROM_FIELD,
                                    Resource.objects.get(pk=baseId),
                                )
                                setattr(rxr, RXR_TILE_FIELD, base)
                                self._resource_x_resource_save(rxr)
                            baseInfo[key].append(info)
                        else:
                            for rxr in ResourceXResource.objects.filter(
                                resourcexid=info["resourceXresourceId"]
                            ):
                                self._resource_x_resource_delete(rxr)

        base.data = baseInfo
        self._tile_save(base)

    def merge_reference_preview(
        self,
        baseId: str,
        valueMerge: Tile,
        base: Tile,
        source_references_id: str,
        keys: list[str] | None,
    ):
        if not keys:
            return [], [], [], []
        added_tile = []
        removed_tile = []
        resource_id_added = []
        resource_id_merged = []
        listReference = self.all_list_reference(keys, base)
        valueInfo = self._get_tile_data(valueMerge)
        baseInfo = self._get_tile_data(base)
        for key in keys:
            if valueInfo[key] is None:
                continue
            if isinstance(valueInfo[key], list):
                for info in valueInfo[key]:
                    if "resourceId" in info:
                        if info["resourceId"] not in listReference:
                            for rxr in ResourceXResource.objects.filter(
                                resourcexid=info["resourceXresourceId"]
                            ):
                                setattr(
                                    rxr,
                                    RXR_FROM_FIELD,
                                    Resource.objects.get(pk=baseId),
                                )
                                setattr(rxr, RXR_TILE_FIELD, base)

                            baseInfo[key].append(info)
                            added_tile.append(str(valueMerge.tileid))
                            if key == source_references_id:
                                resource_id_added.append(info["resourceId"])
                    elif info not in baseInfo[key]:
                        if key == source_references_id:
                            resource_id_merged.append(info["resourceId"])

        return added_tile, removed_tile, resource_id_added, resource_id_merged

    def check_information_preview(
        self,
        matching_row: list[Tile],
        value: Tile,
        keys: list[str],
        base: str,
        flagParent: bool,
        source_references_id: str,
        childtileid: str | None = None,
        matching_child: list[Tile] | None = None,
        childKeys: list[str] | None = None,
    ):
        added_tile = []
        removed_tile = []
        resource_id_added = []
        resource_id_merged = []
        parent_tile_exists = Tile.objects.filter(tileid=value.tileid).exists()
        key_of_string_value = self.check_if_values_contain_string_value([value], keys)

        if parent_tile_exists and not flagParent and not key_of_string_value:
            if not matching_row:
                for tile in Tile.objects.filter(tileid=value.tileid):
                    added_tile.append(str(tile.tileid))
            else:
                order = max((row.sortorder or 0) for row in matching_row) + 1
                added, removed, added_reference_id, merged_reference_id = (
                    self.check_for_same_information_preview(
                        value=value,
                        matching_tiles=matching_row,
                        keys=keys,
                        flag_label=key_of_string_value is None,
                        order=order,
                        baseId=base,
                        source_references_id=source_references_id,
                    )
                )
                added_tile.extend(added)
                removed_tile.extend(removed)
                resource_id_added.extend(added_reference_id)
                resource_id_merged.extend(merged_reference_id)
        else:
            info = self._get_tile_data(value)

            if flagParent:
                if matching_row == [] and childtileid:
                    added_tile.append(str(childtileid))
                else:
                    if not key_of_string_value:
                        added_tile.append(str(childtileid))
                    else:
                        for tile in matching_row:
                            infoName = self._get_tile_data(tile)
                            if infoName[key_of_string_value] is None:
                                continue
                            if (
                                info[key_of_string_value]["en"]
                                == infoName[key_of_string_value]["en"]
                            ):
                                baseInfo = Tile.objects.get(pk=childtileid)
                                if matching_child:
                                    for child in matching_child:
                                        if (
                                            child.parenttile
                                            and child.parenttile.tileid == tile.tileid
                                        ):
                                            if (
                                                childKeys
                                                and self._has_distinct_single_reference(
                                                    child, baseInfo, childKeys
                                                )
                                            ):
                                                continue
                                            (
                                                added,
                                                removed,
                                                resource_id_added,
                                                resource_id_merged,
                                            ) = self.merge_reference_preview(
                                                base,
                                                baseInfo,
                                                child,
                                                source_references_id,
                                                childKeys,
                                            )
                                            added_tile.extend(added)
                                            removed_tile.extend(removed)
                                            return (
                                                added_tile,
                                                removed_tile,
                                                resource_id_added,
                                                resource_id_merged,
                                            )
                        added_tile.append(str(childtileid))

            else:
                if matching_row == []:
                    added_tile.append(str(value.tileid))
                else:
                    if not key_of_string_value:
                        order = max(row.sortorder or 0 for row in matching_row) + 1
                        key_of_string_value = self.check_if_values_contain_string_value(
                            matching_row, keys
                        )
                        if key_of_string_value:
                            added_tile.append(str(value.tileid))
                        else:
                            (
                                added,
                                removed,
                                added_reference_id,
                                merged_reference_id,
                            ) = self.check_for_same_information_preview(
                                value=value,
                                matching_tiles=matching_row,
                                keys=keys,
                                flag_label=key_of_string_value is None,
                                order=order,
                                baseId=base,
                                source_references_id=source_references_id,
                            )
                            added_tile.extend(added)
                            removed_tile.extend(removed)
                            resource_id_added.extend(added_reference_id)
                            resource_id_merged.extend(merged_reference_id)
                    else:
                        for name in matching_row:
                            nameInfo = self._get_tile_data(name)
                            if nameInfo.get(key_of_string_value) is None:
                                continue
                            if (
                                info[key_of_string_value]["en"]
                                == nameInfo[key_of_string_value]["en"]
                            ):
                                if self._has_distinct_single_reference(
                                    name, value, keys
                                ):
                                    continue
                                (
                                    added,
                                    removded,
                                    resource_id_added,
                                    resource_id_merged,
                                ) = self.merge_reference_preview(
                                    base, value, name, source_references_id, keys
                                )
                                added_tile.extend(added)
                                removed_tile.extend(removded)
                                return (
                                    added_tile,
                                    removed_tile,
                                    resource_id_added,
                                    resource_id_merged,
                                )
                        added_tile.append(str(value.tileid))
        return added_tile, removed_tile, resource_id_added, resource_id_merged

    def check_information(
        self,
        matching_row: list[Tile],
        value: Tile,
        keys: list[str],
        base: str,
        flagParent: bool,
        childtileid: str | None = None,
        matching_child: list[Tile] | None = None,
        childKeys: list[str] | None = None,
    ):
        parent_tile_exists = Tile.objects.filter(tileid=value.tileid).exists()
        key_of_string_value = self.check_if_values_contain_string_value([value], keys)

        if parent_tile_exists and not flagParent and not key_of_string_value:
            if not matching_row:
                for rxr in ResourceXResource.objects.filter(
                    **{RXR_TILE_FIELD: value.tileid}
                ):
                    setattr(
                        rxr,
                        RXR_FROM_FIELD,
                        Resource.objects.get(pk=base),
                    )
                    self._resource_x_resource_save(rxr)
                for tile in Tile.objects.filter(tileid=value.tileid):
                    tile.resourceinstance = Resource.objects.get(pk=base)
                    self._tile_save(tile)
            else:
                order = max((row.sortorder or 0) for row in matching_row) + 1
                _ = self.check_for_same_information(
                    value,
                    matching_row,
                    keys,
                    flag_label=key_of_string_value is None,
                    order=order,
                    baseId=base,
                )
        else:
            info = self._get_tile_data(value)

            if flagParent:
                if matching_row == [] and childtileid:
                    baseInfo = Tile.objects.get(tileid=childtileid)
                    self.update_value(base, baseInfo, 0, childKeys)
                else:
                    matched_parent_tile = self._find_matching_tile(
                        value, matching_row, keys
                    )
                    if not key_of_string_value:
                        order = 0
                        if matching_child:
                            order = (
                                max(row.sortorder or 0 for row in matching_child) + 1
                            )
                        baseInfo = Tile.objects.get(pk=childtileid)
                        self.update_value(
                            base,
                            baseInfo,
                            order,
                            childKeys,
                            parenttile=matched_parent_tile,
                        )
                    else:
                        for tile in matching_row:
                            infoName = self._get_tile_data(tile)
                            if infoName[key_of_string_value] is None:
                                continue
                            if (
                                info[key_of_string_value]["en"]
                                == infoName[key_of_string_value]["en"]
                            ):
                                baseInfo = Tile.objects.get(pk=childtileid)
                                if matching_child:
                                    for child in matching_child:
                                        if (
                                            child.parenttile
                                            and child.parenttile.tileid == tile.tileid
                                        ):
                                            if (
                                                childKeys
                                                and self._has_distinct_single_reference(
                                                    child, baseInfo, childKeys
                                                )
                                            ):
                                                continue
                                            self.merge_reference(
                                                base, baseInfo, child, childKeys
                                            )
                                            return None
                                order = baseInfo.sortorder or 0
                                # Reparent the moved child to the matched base
                                # parent so it survives cleanup of the merge
                                # resource tiles.
                                self.update_value(
                                    base,
                                    baseInfo,
                                    order,
                                    childKeys,
                                    parenttile=tile,
                                )
                                return None
                        baseInfo = Tile.objects.get(pk=childtileid)
                        order = baseInfo.sortorder or 0
                        self.update_value(base, baseInfo, order, childKeys)

            else:
                if matching_row == []:
                    self.update_value(base, value, 0, keys)
                else:
                    if not key_of_string_value:
                        order = max(row.sortorder or 0 for row in matching_row) + 1
                        key_of_string_value = self.check_if_values_contain_string_value(
                            matching_row, keys
                        )
                        if key_of_string_value:
                            self.update_value(base, value, order, keys)
                        else:
                            _ = self.check_for_same_information(
                                value,
                                matching_row,
                                keys,
                                flag_label=key_of_string_value is None,
                                order=order,
                                baseId=base,
                            )
                    else:
                        for name in matching_row:
                            nameInfo = self._get_tile_data(name)
                            if nameInfo.get(key_of_string_value) is None:
                                continue
                            if (
                                info[key_of_string_value]["en"]
                                == nameInfo[key_of_string_value]["en"]
                            ):
                                if self._has_distinct_single_reference(
                                    name, value, keys
                                ):
                                    continue
                                self.merge_reference(base, value, name, keys)
                                return None
                        order = max((row.sortorder or 0) for row in matching_row) + 1
                        self.update_value(base, value, order, keys)

    def write(self, request: HttpRequest):
        base_resource = request.POST.get("resourceBase", None)
        merge_resources = request.POST.get("mergeResources", "").split(",")

        load_details = {
            "baseResource": base_resource,
            "mergeResources": merge_resources,
        }

        with connection.cursor() as cursor:
            event_created = self.create_load_event(cursor, load_details)
            if event_created["success"]:
                response = self.run_bulk_task_async(request, self.loadid)
            else:
                self.log_event(cursor, "failed")
                return {"success": False, "data": event_created["message"]}
        return response

    def _process_merge_tile_preview(
        self,
        resource: str,
        tile: Tile,
        base_resource: str,
        base_tiles: list[Tile],
        source_references_id: str,
    ):

        added_tile = []
        removed_tile = []
        resource_id_added = []
        resource_id_merged = []
        nodegroupid = str(tile.nodegroup.nodegroupid)

        if not self.nodegroups.get(nodegroupid):
            self.nodegroups[nodegroupid] = NodeGroup.objects.get(
                nodegroupid=nodegroupid
            )

        cardinality = str(self.nodegroups[nodegroupid].cardinality)
        parent_tile_id: str | None = (
            str(tile.parenttile.tileid) if tile.parenttile else None
        )
        # base_tiles einai auta pou erxountai apo to base resource kai sta matching_tiles_by_nodegroups kratame ta tiles pou exoun to sugkekrimeno nodegroupid opws auta
        # apo to tile tou merge resource pou koitame
        matching_tiles_by_nodegroups = [
            same for same in base_tiles if same.nodegroup == tile.nodegroup
        ]

        information = self._get_tile_data(tile)
        keys = list(
            information.keys()
        )  # einai ta node pou exei mesa to tile mesa se auto to nodegroupid ta keys dld sto tiledata
        logger.warning(f"Tile {tile.tileid} has keys {keys}")
        logger.warning(f"{base_resource}")

        if cardinality != "1" or parent_tile_id is not None:
            if parent_tile_id is None:
                added, removed, resource_id_added_, resource_id_merged_ = (
                    self.check_information_preview(
                        matching_row=matching_tiles_by_nodegroups,
                        value=tile,
                        keys=keys,
                        base=base_resource,
                        flagParent=False,
                        source_references_id=source_references_id,
                    )
                )
                added_tile.extend(added)
                removed_tile.extend(removed)
                resource_id_added.extend(resource_id_added_)
                resource_id_merged.extend(resource_id_merged_)

                return added_tile, removed_tile, resource_id_added, resource_id_merged

            parent_tiles = Tile.objects.filter(tileid=parent_tile_id)

            for parent_tile in parent_tiles:
                matching_row = [
                    same
                    for same in base_tiles
                    if same.nodegroup == parent_tile.nodegroup
                ]
                parent_keys: list[str] = list((self._get_tile_data(parent_tile)).keys())
                added, removed, resource_id_added_, resource_id_merged_ = (
                    self.check_information_preview(
                        matching_row=matching_row,
                        value=parent_tile,  # it comes from tile which comes from the merge resource
                        keys=parent_keys,
                        base=base_resource,
                        flagParent=True,
                        childtileid=str(
                            tile.tileid
                        ),  # it comes also from the merge resource
                        matching_child=matching_tiles_by_nodegroups,
                        childKeys=keys,
                        source_references_id=source_references_id,
                    )
                )
                added_tile.extend(added)
                removed_tile.extend(removed)
                resource_id_added.extend(resource_id_added_)
                resource_id_merged.extend(resource_id_merged_)

        else:
            tiles_with_matching_nodegroup = [
                same for same in base_tiles if same.nodegroup == tile.nodegroup
            ]
            if tiles_with_matching_nodegroup == []:
                added_tile.append(str(tile.tileid))
            else:
                for matching_tile in tiles_with_matching_nodegroup:
                    added_tile.append(str(matching_tile.tileid))
        return added_tile, removed_tile, resource_id_added, resource_id_merged

    def _process_merge_tile(
        self,
        resource: str,
        tile: Tile,
        base_resource: str,
        base_tiles: list[Tile],
    ):

        nodegroupid = str(tile.nodegroup.nodegroupid)

        if not self.nodegroups.get(nodegroupid):
            self.nodegroups[nodegroupid] = NodeGroup.objects.get(
                nodegroupid=nodegroupid
            )

        cardinality = str(self.nodegroups[nodegroupid].cardinality)

        parent_tile_id: str | None = (
            str(tile.parenttile.tileid) if tile.parenttile else None
        )
        # base_tiles einai auta pou erxountai apo to base resource kai sta matching_tiles_by_nodegroups kratame ta tiles pou exoun to sugkekrimeno nodegroupid opws auta
        # apo to tile tou merge resource pou koitame
        matching_tiles_by_nodegroups = [
            same for same in base_tiles if same.nodegroup == tile.nodegroup
        ]

        information = self._get_tile_data(tile)
        keys = list(
            information.keys()
        )  # einai ta node pou exei mesa to tile mesa se auto to nodegroupid ta keys dld sto tiledata

        if cardinality != "1" or parent_tile_id is not None:
            if parent_tile_id is None:
                self.check_information(
                    matching_tiles_by_nodegroups,
                    tile,
                    keys,
                    base_resource,
                    False,
                )
                return

            parent_tiles = Tile.objects.filter(tileid=parent_tile_id)

            for parent_tile in parent_tiles:
                matching_row = [
                    same
                    for same in base_tiles
                    if same.nodegroup == parent_tile.nodegroup
                ]
                parent_keys: list[str] = list((self._get_tile_data(parent_tile)).keys())
                self.check_information(
                    matching_row,
                    parent_tile,
                    parent_keys,
                    base_resource,
                    True,
                    childtileid=str(tile.tileid),
                    matching_child=matching_tiles_by_nodegroups,
                    childKeys=keys,
                )

        else:
            tiles_with_matching_nodegroup = [
                same for same in base_tiles if same.nodegroup == tile.nodegroup
            ]
            if tiles_with_matching_nodegroup == []:
                for rxr in ResourceXResource.objects.filter(
                    **{RXR_TILE_FIELD: tile.tileid}
                ):
                    setattr(
                        rxr,
                        RXR_FROM_FIELD,
                        Resource.objects.get(pk=base_resource),
                    )
                    self._resource_x_resource_save(rxr)

                tile.resourceinstance = Resource.objects.get(pk=base_resource)
                self._tile_save(tile)
            else:
                for rxr in ResourceXResource.objects.filter(
                    **{RXR_TILE_FIELD: tile.tileid}
                ):
                    self._resource_x_resource_delete(rxr)

                for matching_tile in tiles_with_matching_nodegroup:
                    tile.parenttile = matching_tile
                    self._tile_save(tile)

    def _process_merge_resource_preview(
        self,
        resource: str,
        base_resource: str,
        base_tiles: list[Tile],
        source_references_id: str,
        excluded_tile_ids: list[str] | None = None,
    ):
        if excluded_tile_ids is None:
            excluded_tile_ids = []

        merge_resource_tiles = Tile.objects.filter(resourceinstance=resource)
        added_tile = []
        removed_tile = []
        resource_id_added = []
        resource_id_merged = []

        for merge_resource_tile in merge_resource_tiles:
            if str(merge_resource_tile.tileid) in excluded_tile_ids:
                continue
            added, removed, resource_id_added_, resource_id_merged_ = (
                self._process_merge_tile_preview(
                    resource,
                    merge_resource_tile,
                    base_resource,
                    base_tiles,
                    source_references_id,
                )
            )

            added_tile.extend(added)
            removed_tile.extend(removed)
            resource_id_added.extend(resource_id_added_)
            resource_id_merged.extend(resource_id_merged_)

        for merge_resource_tile in merge_resource_tiles:
            if (
                merge_resource_tile.parenttile is not None
                and str(merge_resource_tile.tileid) in added_tile
            ):
                if str(merge_resource_tile.parenttile.tileid) not in added_tile:
                    added_tile.remove(str(merge_resource_tile.tileid))
        return added_tile, removed_tile, resource_id_added, resource_id_merged

    def _process_merge_resource(
        self,
        resource: str,
        base_resource: str,
        base_tiles: list[Tile],
        excluded_tile_ids: list[str] | None = None,
    ):
        log_event_details(self.loadid, f"|Merging resource {resource}")
        if excluded_tile_ids is None:
            excluded_tile_ids = []
        merge_resource_tiles = Tile.objects.filter(resourceinstance=resource)
        for merge_resource_tile in merge_resource_tiles:
            if str(merge_resource_tile.tileid) in excluded_tile_ids:
                continue
            self._process_merge_tile(
                resource, merge_resource_tile, base_resource, base_tiles
            )

    @staticmethod
    def get_tiles_value():
        with connection.cursor() as cursor:
            cursor.execute(
                """select *
                from tiles
                where resourceinstanceid ='f4e3e7d5-7f70-32b6-8377-1ff48d60d91a' and nodegroupid='48d04315-747d-11ec-b195-0a9473e82189'"""
            )

            rows = cursor.fetchall()

            cursor.execute(
                """select *
                    from tiles
                    where tileid='721dcdf6-cfdb-43fa-9bf6-5ad990907932'"""
            )

            rows = cursor.fetchall()

    @staticmethod
    def _count_related_resources(resourceid: str) -> tuple[int, list[str]]:
        with connection.cursor() as cursor:
            cursor.execute(
                """(
                    SELECT
                        ri.name,
                        rxr.resourceinstanceidfrom AS related_resource_id
                    FROM resource_x_resource rxr
                    JOIN resource_instances ri
                        ON ri.resourceinstanceid = rxr.resourceinstanceidfrom
                    WHERE rxr.resourceinstanceidto = %s
                )
                UNION
                (
                    SELECT
                        ri.name,
                        rxr.resourceinstanceidto AS related_resource_id
                    FROM resource_x_resource rxr
                    JOIN resource_instances ri
                        ON ri.resourceinstanceid = rxr.resourceinstanceidto
                    WHERE rxr.resourceinstanceidfrom = %s
                );""",
                (resourceid, resourceid),
            )

            rows = cursor.fetchall()

            # Extract only related_resource_id column (index 1)
            related_ids = [row[1] for row in rows]

            return len(related_ids), related_ids

    def _get_tiles_info(self, merge_resources, base_resource, excluded_tile_ids):
        resource = Resource.objects.get(pk=base_resource)
        graph = Graph.objects.get(graphid=resource.graph_id)
        source_references_id = (
            str(self.get_source_references_ids(graph.graphid)[0])
            if self.get_source_references_ids(graph.graphid)
            else "0"
        )
        node_group_ids: set[str] = set()
        tiles_per_nodegroup: dict[str, set[Tile]] = {}
        for merge_resource in merge_resources:
            merge_resource_tiles = list(
                Tile.objects.filter(resourceinstance=merge_resource)
            )
            for merge_tile in merge_resource_tiles:
                if str(merge_tile.nodegroup.nodegroupid) not in tiles_per_nodegroup:
                    tiles_per_nodegroup[str(merge_tile.nodegroup.nodegroupid)] = set()

                node_group_ids.add(str(merge_tile.nodegroup.nodegroupid))
                tiles_per_nodegroup[str(merge_tile.nodegroup.nodegroupid)].add(
                    merge_tile
                )

        node_groups_to_return: set[str] = set(node_group_ids).union(
            set(tiles_per_nodegroup.keys())
        )
        node_group_details: list[dict[str, object]] = []
        for node_group_id in sorted(
            node_groups_to_return, key=self._get_nodegroup_preview_sort_key
        ):
            meta = self._get_nodegroup_preview_meta(node_group_id)
            nodegroup = meta["nodegroup"]
            branch_name = meta["branch_name"]
            card_name = meta["card_name"]

            tile_details: list[dict[str, str]] = []
            tile_set = tiles_per_nodegroup.get(str(node_group_id), set())
            for tile in sorted(tile_set, key=lambda t: str(t.tileid)):
                tiledata = []

                for nodeid, _ in self._get_ordered_tile_data_items(tile):
                    node = self.get_node(nodeid)
                    nod = models.Node.objects.get(pk=nodeid)
                    widget_label = self._get_widget_label(node, nod)
                    datatype = self.datatype_factory.get_instance(node.datatype)
                    if nodeid == source_references_id:
                        values = self.get_display_values_source_reference(
                            tile, node, datatype, None
                        )

                        for value in values:
                            tiledata.append(
                                (widget_label, value[0], value[1], value[2])
                            )
                    else:
                        tiledata.append(
                            (widget_label, datatype.get_display_value(tile, node))
                        )
                flag = "included"
                if str(tile.tileid) in excluded_tile_ids:
                    flag = "excluded"

                tile_details.append(
                    {
                        "tileId": str(tile.tileid),
                        "flag": flag,
                        "resourceId": str(tile.resourceinstance.resourceinstanceid),
                        "tiledata": tiledata,
                        "status": "Unknown",
                    }
                )

            node_group_details.append(
                {
                    "nodegroupId": str(node_group_id),
                    "name": card_name,
                    "branch_name": branch_name,
                    "tiles": tile_details,
                }
            )
        return node_group_details, source_references_id

    def update_tile_status(
        self,
        data,
        tile_ids_to_change,
        source_references_id,
        related_ids_pre_merge,
        list_of_source_references_ids,
    ):

        tile_ids_to_change = set(tile_ids_to_change)

        for ng in data:
            list_of_source_references_ids = [
                str(id) for id in list_of_source_references_ids
            ]
            related_ids_pre_merge = [str(id) for id in related_ids_pre_merge]

            for tile in ng["tiles"]:
                if tile["tileId"] in tile_ids_to_change:
                    tile["status"] = "added"
                else:
                    tile["status"] = "deleted"

                if ng["nodegroupId"] == source_references_id:
                    count = 0

                    for i, t in enumerate(tile["tiledata"]):
                        if (
                            t[2] not in related_ids_pre_merge
                            and t[2] in list_of_source_references_ids
                        ):
                            tile["tiledata"][i] = (t[0], t[1], "added")
                            count += 1
                        else:
                            tile["tiledata"][i] = (t[0], t[1], "deleted")

                    if count < len(tile["tiledata"]):
                        tile["status"] = "deleted"
                    else:
                        tile["status"] = "added"

        return data

    @staticmethod
    def _count_source_references(
        resourceid: str, excluded_tiles=None
    ) -> tuple[int, list[str]]:

        import json

        if excluded_tiles is None:
            excluded_tiles = []

        # if excluded_tiles is a string, try to parse it as JSON
        if isinstance(excluded_tiles, str):
            excluded_tiles = json.loads(excluded_tiles)

        # optional: enforce list
        if not isinstance(excluded_tiles, (list, tuple)):
            raise ValueError(f"excluded_tiles must be list, got {type(excluded_tiles)}")
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT
                    jsonb_path_query(tiledata::jsonb, '$.**.resourceId') #>> '{}' AS resource_id
                FROM tiles
                WHERE tiledata::text ILIKE %s
                and nodegroupid in (select nodegroupid
                FROM nodes
                where name= 'source_reference')
                AND resourceinstanceid = %s
                 AND tileid != ALL(%s::uuid[]);""",
                (
                    "%resourceId%",
                    resourceid,
                    excluded_tiles,
                ),
            )

            rows = cursor.fetchall()

            # Extract only related_resource_id column (index 1)
            related_ids = [row[0] for row in rows]

            return len(related_ids), related_ids

    @staticmethod
    def _count_source_references_excluded_tiles(
        resourceid: str, excluded_tiles=None
    ) -> tuple[int, list[str]]:

        if excluded_tiles is None:
            excluded_tiles = []

        if isinstance(excluded_tiles, str):
            excluded_tiles = json.loads(excluded_tiles)

        excluded_tiles = list(excluded_tiles)

        with connection.cursor() as cursor:
            query = """SELECT DISTINCT
                jsonb_path_query(tiledata::jsonb, '$.**.resourceId') #>> '{}' AS resource_id
            FROM tiles
            WHERE tiledata::text ILIKE %s
            AND nodegroupid IN (
                SELECT nodegroupid
                FROM nodes
                WHERE name = 'source_reference'
            )
            AND resourceinstanceid = %s
            """

            params = ["%resourceId%", resourceid]

            # ONLY add filter if list is not empty
            if excluded_tiles:
                query += " AND tileid = ANY(%s::uuid[])"
                params.append(excluded_tiles)

            cursor.execute(query, params)

            rows = cursor.fetchall()
            related_ids = [row[0] for row in rows]

            return len(related_ids), related_ids

    @staticmethod
    def _count_source_references_tile(tileid: str) -> tuple[int, list[str]]:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT
                    jsonb_path_query(tiledata::jsonb, '$.**.resourceId') #>> '{}' AS resource_id
                FROM tiles
                WHERE tiledata::text ILIKE %s
                AND tileid = %s;""",
                (
                    "%resourceId%",
                    tileid,
                ),
            )

            rows = cursor.fetchall()

            # Extract only related_resource_id column (index 1)
            related_ids = [row[0] for row in rows]

            return len(set(related_ids)), list(set(related_ids))

    @staticmethod
    def get_source_references_ids(graphid):
        with connection.cursor() as cursor:
            cursor.execute(
                """select nodeid
                    from nodes
                    where nodes.name like 'source_reference' and nodes.graphid= %s """,
                (graphid,),
            )

            rows = cursor.fetchall()

            # Extract nodeids
            source_references_ids = [row[0] for row in rows]

            return source_references_ids

    def calculate_number_of_tiles_per_nodegroup_baseresource(
        self, resource, dict_counts, source_references_id, related_ids, prefix="_pre"
    ):
        name = str(resource) + prefix
        source_reference_card_name = None
        resource_tiles = list(Tile.objects.filter(resourceinstance=resource))
        for tile in resource_tiles:
            card_name, node_group_id = self.get_name_card(tile)

            dict_counts.setdefault(card_name, {})
            dict_counts[card_name].setdefault(name, 0)

            if node_group_id != source_references_id:
                dict_counts[card_name][name] += 1
            else:
                dict_counts[card_name][name] = len(set(related_ids))
                source_reference_card_name = card_name

        return dict_counts, source_reference_card_name

    @staticmethod
    def update_dictionaries(
        dict_tobe_updated,
        card_name,
        name,
        related_ids,
        source_references_id,
        node_group_id,
        tile,
    ):
        source_reference_card_name = None
        # ensure card_name exists
        if card_name not in dict_tobe_updated:
            dict_tobe_updated[card_name] = {}

        # SOURCE REFERENCE CASE
        if node_group_id == source_references_id:
            dict_tobe_updated[card_name][name] = set(related_ids)
            source_reference_card_name = card_name

        # NORMAL CASE
        else:
            # ensure list exists
            if name not in dict_tobe_updated[card_name]:
                dict_tobe_updated[card_name][name] = []

            dict_tobe_updated[card_name][name].append(str(tile.tileid))
        return source_reference_card_name

    def build_dict_merge_resources_tiles_per_nodegroup(
        self,
        resource,
        dict_counts,
        dict_counts_excluded,
        source_references_id,
        related_ids,
        related_excluded_ids,
        excluded_tiles,
    ):
        name = str(resource)
        source_reference_card_name = None

        resource_tiles = list(Tile.objects.filter(resourceinstance=resource))

        for tile in resource_tiles:
            card_name, node_group_id = self.get_name_card(tile)

            if str(tile.tileid) in excluded_tiles:
                temp_source_reference_card_name = self.update_dictionaries(
                    dict_counts_excluded,
                    card_name,
                    name,
                    related_excluded_ids,
                    source_references_id,
                    node_group_id,
                    tile,
                )
            else:
                temp_source_reference_card_name = self.update_dictionaries(
                    dict_counts,
                    card_name,
                    name,
                    related_ids,
                    source_references_id,
                    node_group_id,
                    tile,
                )

            if temp_source_reference_card_name is not None:
                source_reference_card_name = temp_source_reference_card_name

        return dict_counts, dict_counts_excluded, source_reference_card_name

    def calculate_number_added_deleted_merge_resource(
        self,
        post_base_tiles,
        dict_merge_resources,
        dict_merge_resources_excluded,
        dict_count,
        source_reference_card_name,
        pre_baseresource_reference_ids,
    ):

        if pre_baseresource_reference_ids is None:
            pre_baseresource_reference_ids = set()

        for card_name in dict_merge_resources:
            if card_name not in dict_count:
                dict_count[card_name] = {}
            for merge_resource in dict_merge_resources[card_name]:
                if card_name == source_reference_card_name:
                    total_tiles = len(dict_merge_resources[card_name][merge_resource])
                    added_tiles = len(
                        set(dict_merge_resources[card_name][merge_resource])
                        - set(pre_baseresource_reference_ids)
                    )
                    deleted_tiles = total_tiles - added_tiles
                    dict_count[card_name][merge_resource + "_added"] = added_tiles
                    dict_count[card_name][merge_resource + "_deleted"] = deleted_tiles
                else:
                    total_tiles = len(dict_merge_resources[card_name][merge_resource])
                    added_tiles = len(
                        set(dict_merge_resources[card_name][merge_resource])
                        & set(post_base_tiles)
                    )
                    deleted_tiles = total_tiles - added_tiles
                    dict_count[card_name][merge_resource + "_added"] = added_tiles
                    dict_count[card_name][merge_resource + "_deleted"] = deleted_tiles

        for card_name in dict_merge_resources_excluded:
            if card_name not in dict_count:
                dict_count[card_name] = {}
            for merge_resource in dict_merge_resources_excluded[card_name]:
                dict_count[card_name][merge_resource + "_excluded"] = len(
                    dict_merge_resources_excluded[card_name][merge_resource]
                )

        return dict_count

    @staticmethod
    def _normalize_counts(data):
        # 1. Collect all possible keys
        all_keys = set()
        for counts in data.values():
            all_keys.update(counts.keys())

        # 2. Build a new normalized dictionary
        normalized = {
            resource: {key: counts.get(key, 0) for key in all_keys}
            for resource, counts in data.items()
        }

        return normalized

    @load_data_async
    def run_bulk_task_async(self, request: HttpRequest):
        base_resource = request.POST.get("resourceBase", None)
        merge_resources = request.POST.get("mergeResources", "").split(",")
        excluded_tile_ids = request.POST.get("excluded_tile_ids", [])

        edit_task = tasks.bulk_data_merge_resources.apply_async(
            (
                self.userid,
                self.loadid,
                base_resource,
                merge_resources,
                excluded_tile_ids,
            ),
        )
        event = LoadEvent.objects.get(loadid=self.loadid)
        event.taskid = edit_task.task_id
        event.save()

    @transaction.atomic
    def run_load_task(
        self,
        userid: str,
        loadid: str,
        base_resource: str,
        merge_resources: list[str],
        excluded_tile_ids: list[str] | None = None,
    ):
        if not self.user:
            self.user = User.objects.get(pk=userid)

        if excluded_tile_ids is None:
            excluded_tile_ids = []
        try:
            with transaction.atomic():
                logger.warning(
                    f"Merge Resources started for {base_resource} with {', '.join(merge_resources)}"
                )
                log_event_details(
                    loadid,
                    f"|Merge Resources started for {base_resource} with {', '.join(merge_resources)}",
                )
                base_resource_tiles = list(
                    Tile.objects.filter(resourceinstance=base_resource)
                )

                total_merge_counts = []
                total_source_reference_counts = []
                resourceids_of_related_resources = set()
                resourceids_of_source_references = set()
                initial_nodegroup_details, source_references_id = self._get_tiles_info(
                    merge_resources, base_resource, excluded_tile_ids
                )
                # count related resources base before merge
                count_of_related_resources, related_ids = self._count_related_resources(
                    base_resource
                )
                resourceids_of_related_resources.update(related_ids)
                total_merge_counts.append((base_resource, count_of_related_resources))
                # count source references base before merge
                count_of_source_references, related_ids = self._count_source_references(
                    base_resource
                )
                related_ids_pre_merge = [str(id) for id in related_ids]
                resourceids_of_source_references.update(related_ids)
                total_source_reference_counts.append(
                    (base_resource, count_of_source_references)
                )
                dict_counts = {}

                dict_counts, source_reference_card_name = (
                    self.calculate_number_of_tiles_per_nodegroup_baseresource(
                        base_resource,
                        dict_counts,
                        source_references_id,
                        related_ids_pre_merge,
                        "_pre",
                    )
                )
                dict_merge_resources = {}
                dict_merge_resources_excluded = {}
                for merge_resource in merge_resources:
                    # count related resources merge resource
                    count_of_related_resources, related_ids = (
                        self._count_related_resources(merge_resource)
                    )
                    resourceids_of_related_resources.update(related_ids)
                    total_merge_counts.append(
                        (merge_resource, count_of_related_resources)
                    )

                    # count source references merge resource
                    count_of_source_references, related_ids = (
                        self._count_source_references(
                            merge_resource, excluded_tiles=excluded_tile_ids
                        )
                    )

                    _, related_ids_excluded = (
                        self._count_source_references_excluded_tiles(
                            merge_resource, excluded_tiles=excluded_tile_ids
                        )
                    )
                    (
                        dict_merge_resources,
                        dict_merge_resources_excluded,
                        source_reference_card_name,
                    ) = self.build_dict_merge_resources_tiles_per_nodegroup(
                        merge_resource,
                        dict_merge_resources,
                        dict_merge_resources_excluded,
                        source_references_id,
                        related_ids,
                        related_ids_excluded,
                        excluded_tile_ids,
                    )
                    resourceids_of_source_references.update(related_ids)
                    total_source_reference_counts.append(
                        (merge_resource, count_of_source_references)
                    )

                    self._process_merge_resource(
                        merge_resource,
                        base_resource,
                        base_resource_tiles,
                        excluded_tile_ids,
                    )

                log_event_details(loadid, "|Done, deleting old resources...")
                logger.warning("Done, deleting old resources...")

                for merge_resource in merge_resources:
                    relations = ResourceXResource.objects.filter(
                        **{RXR_TO_FIELD: merge_resource}
                    )
                    relation_tile_ids: list[str] = list(
                        map(lambda x: str(getattr(x, RXR_TILE_FIELD)), relations)
                    )
                    for relation_tile_id in relation_tile_ids:
                        match = re.search(r"\((.*?)\)", relation_tile_id)
                        relation_tile = Tile.objects.get(tileid=match.group(1))
                        infoTiledata = self._get_tile_data(relation_tile)

                        for key in infoTiledata:
                            if isinstance(infoTiledata[key], list):
                                for resources in infoTiledata[key]:
                                    if "resourceId" in resources:
                                        if resources["resourceId"] == merge_resource:
                                            resources["resourceId"] = base_resource
                            elif isinstance(infoTiledata[key], dict):
                                if "resourceId" in infoTiledata:
                                    if (
                                        infoTiledata[key]["resourceId"]
                                        == merge_resource
                                    ):
                                        infoTiledata["resourceId"] = base_resource

                        relation_tile.data = infoTiledata
                        self._tile_save(relation_tile)

                    for rxr in relations:
                        setattr(
                            rxr,
                            RXR_TO_FIELD,
                            Resource.objects.get(pk=base_resource),
                        )
                        self._resource_x_resource_save(rxr)

                    result = list(Tile.objects.filter(resourceinstance=merge_resource))

                    for tile in result:
                        tile.data = {}
                        self._tile_save(tile)

                    for rxr in ResourceXResource.objects.filter(
                        **{RXR_FROM_FIELD: merge_resource}
                    ):
                        self._resource_x_resource_delete(rxr)
                    _ = GeoJSONGeometry.objects.filter(
                        resourceinstance=merge_resource
                    ).delete()
                    tiles = Tile.objects.filter(resourceinstance=merge_resource)

                    self.get_tiles_value()
                    for tile in tiles:
                        self._tile_delete(tile)

                    self.get_tiles_value()
                    self._resource_delete(Resource.objects.get(pk=merge_resource))

                    self.get_tiles_value()
                # count related resources base after merge
                count_of_related_resources, _ = self._count_related_resources(
                    base_resource
                )

                post_merge_tiles = [
                    str(tile.tileid)
                    for tile in Tile.objects.filter(resourceinstance=base_resource)
                ]

                total_merge_counts.append((base_resource, count_of_related_resources))
                # calulate total unique related resources among all resources
                total_merge_counts.append(
                    ("Total Related Resources", len(resourceids_of_related_resources))
                )
                # count source references base after merge
                count_of_source_references, related_ids = self._count_source_references(
                    base_resource
                )

                final_data = self.update_tile_status(
                    initial_nodegroup_details,
                    post_merge_tiles,
                    source_references_id,
                    related_ids_pre_merge,
                    related_ids,
                )

                dict_counts, source_reference_card_name = (
                    self.calculate_number_of_tiles_per_nodegroup_baseresource(
                        base_resource,
                        dict_counts,
                        source_references_id,
                        related_ids,
                        "_post",
                    )
                )
                dict_counts = self.calculate_number_added_deleted_merge_resource(
                    post_merge_tiles,
                    dict_merge_resources,
                    dict_merge_resources_excluded,
                    dict_counts,
                    source_reference_card_name,
                    related_ids_pre_merge,
                )
                dict_counts_normalized = self._normalize_counts(dict_counts)
                total_source_reference_counts.append(
                    (base_resource, count_of_source_references)
                )
                # calulate total unique source references among all resources
                total_source_reference_counts.append(
                    ("Total Source References", len(resourceids_of_source_references))
                )

                event = LoadEvent.objects.get(loadid=loadid)
                event.status = "completed"
                event.load_end_time = datetime.now()
                event.save()
                event.refresh_from_db()

                log_event_details(loadid, "|Done, Indexing...")
                logger.warning("Done, indexing...")
                index_resources_by_transaction(
                    loadid,
                    quiet=True,
                    use_multiprocessing=False,
                    recalculate_descriptors=True,
                )

                logger.warning("Merge Resources completed")
                log_event_details(loadid, "|Done")
                change_summary = self._build_change_summary(base_resource)

                # Add merge counts to the summary
                change_summary["mergeCounts"] = [
                    {"resource": r, "relatedCount": c} for r, c in total_merge_counts
                ]
                # Add merge counts to the summary
                change_summary["mergeCounts"] = [
                    {"resource": r, "relatedCount": c} for r, c in total_merge_counts
                ]
                # Add source reference counts to the summary
                change_summary["sourceReferenceCounts"] = [
                    {"resource": r, "sourceReferenceCount": c}
                    for r, c in total_source_reference_counts
                ]
                if any(change_summary.values()):
                    load_details = event.load_details or {}
                    load_details["changeSummary"] = change_summary
                    load_details["finalData"] = final_data
                    load_details["dictCounts"] = dict_counts_normalized
                    load_details["userNameMerge"] = self.user.username
                    event.load_details = load_details
                event.status = "indexed"
                event.indexed_time = datetime.now()
                event.complete = True
                event.successful = True
                event.save()
                return {"success": True, "data": "done"}
        except Exception as e:
            logger.exception("Exception occurred")

            event = LoadEvent.objects.get(loadid=loadid)
            event.status = "failed"
            event.load_end_time = datetime.now()
            event.save()

            print("Unable to edit staged data: {}".format(str(e)))
            return {
                "success": False,
                "data": {
                    "title": "Error",
                    "message": "Unable to edit staged data: {}".format(str(e)),
                },
            }
