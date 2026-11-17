"""Real ORM/serialization/locking checks; Elasticsearch remains a separate acceptance layer.

Run: python manage.py test tests.integration.merge_roundtrip_tests --settings=tests.test_settings
Django creates its test database; never point this suite at production with a custom runner.
"""
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arches.app.models import models
from arches.app.models.resource import Resource
from arches.app.models.tile import Tile
from arches_resource_merge.etl_modules.Resources_merge import (
    Resourcesmerge, RXR_FROM_FIELD, RXR_TO_FIELD, RXR_TILE_FIELD, RXR_NODE_FIELD,
)


class MergeDatabaseRoundtripTests(TestCase):
    def setUp(self):
        self.graph = models.GraphModel.objects.create(name="Merge integration", isresource=True)
        self.group = models.NodeGroup.objects.create(cardinality="n")
        models.Node.objects.create(graph=self.graph, name="Root", datatype="semantic", istopnode=True)
        self.ref_node = models.Node.objects.create(graph=self.graph, nodegroup=self.group,
                                                  name="References", datatype="resource-instance-list", istopnode=False)
        self.date_node = models.Node.objects.create(graph=self.graph, nodegroup=self.group,
                                                   name="Date", datatype="date", istopnode=False)
        self.resources = [models.ResourceInstance.objects.create(graph=self.graph) for _ in range(4)]
        self.base, self.source, self.peer, self.other = self.resources
        self.ref_ids = [uuid4(), uuid4(), uuid4()]
        self.tiles = []
        for owner, rxid, date in [(self.base, self.ref_ids[0], None),
                                 (self.source, self.ref_ids[1], "1901-01-01")]:
            data = {str(self.ref_node.pk): [{"resourceId": str(self.peer.pk),
                    "resourceXresourceId": str(rxid), "ontologyProperty": "same-property"}],
                    str(self.date_node.pk): date}
            if owner == self.source:
                data[str(self.ref_node.pk)].append({"resourceId": str(self.other.pk),
                    "resourceXresourceId": str(self.ref_ids[2]), "ontologyProperty": "other-property"})
            tile = models.TileModel.objects.create(resourceinstance=owner, nodegroup=self.group, data=data)
            self.tiles.append(tile)
            for reference in data[str(self.ref_node.pk)]:
                target = self.peer if reference["resourceId"] == str(self.peer.pk) else self.other
                models.ResourceXResource.objects.create(resourcexid=reference["resourceXresourceId"], **{
                    RXR_FROM_FIELD: owner, RXR_TO_FIELD: target, RXR_TILE_FIELD: tile,
                    RXR_NODE_FIELD: self.ref_node, "created": timezone.now(), "modified": timezone.now(),
                })
        user = User.objects.create(username="merge-integration")
        module = models.ETLModule.objects.create(name="Merge integration", etl_type="edit")
        self.event = models.LoadEvent.objects.create(user=user, etl_module=module, status="indexed",
                                                    load_details={}, load_end_time=timezone.now())
        self.editor = Resourcesmerge(loadid=str(self.event.pk))
        self.editor.user = user

    def snapshot(self):
        return [list(query.order_by("pk").values()) for query in (
            models.ResourceInstance.objects.filter(pk__in=[r.pk for r in self.resources]),
            models.TileModel.objects.filter(pk__in=[t.pk for t in self.tiles]),
            models.ResourceXResource.objects.filter(pk__in=self.ref_ids),
        )]

    def merge(self):
        with transaction.atomic():
            self.editor.merge_reference(str(self.base.pk), Tile.objects.get(pk=self.tiles[1].pk),
                                        Tile.objects.get(pk=self.tiles[0].pk),
                                        [str(self.ref_node.pk), str(self.date_node.pk)])
            self.editor._tile_delete(Tile.objects.get(pk=self.tiles[1].pk))
            self.editor._resource_delete(Resource.objects.get(pk=self.source.pk))

    def test_tile_data_discovery_refuses_completely_unbacked_incoming_reference(self):
        incoming = models.TileModel.objects.create(
            resourceinstance=self.peer, nodegroup=self.group,
            data={str(self.ref_node.pk): [{"resourceId": str(self.source.pk),
                   "resourceXresourceId": str(uuid4())}]},
        )
        reference = incoming.data[str(self.ref_node.pk)][0]
        for value in (reference, [reference]):
            with self.subTest(value=value):
                before = {str(self.ref_node.pk): value}
                models.TileModel.objects.filter(pk=incoming.pk).update(data=before)
                with self.assertRaisesRegex(ValueError, str(incoming.pk)):
                    self.editor._validate_incoming_references(str(self.base.pk), [str(self.source.pk)])
                self.assertEqual(models.TileModel.objects.get(pk=incoming.pk).data, before)

    def test_reference_merge_and_native_unload_restore_database_exactly(self):
        before = self.snapshot()
        self.merge()
        self.assertFalse(models.ResourceInstance.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(models.TileModel.objects.get(pk=self.tiles[0].pk).data[str(self.date_node.pk)],
                         "1901-01-01")
        self.assertEqual(models.ResourceXResource.objects.filter(pk__in=self.ref_ids).count(), 2)
        moved = models.ResourceXResource.objects.get(pk=self.ref_ids[2])
        self.assertEqual(getattr(moved, RXR_FROM_FIELD+"_id"), self.base.pk)
        self.assertEqual(getattr(moved, RXR_TO_FIELD+"_id"), self.other.pk)
        self.assertEqual(getattr(moved, RXR_TILE_FIELD+"_id"), self.tiles[0].pk)
        with patch.object(self.editor, "_refresh_resource_indexes") as indexing:
            with CaptureQueriesContext(connection) as queries:
                self.editor.reverse_load(str(self.event.pk))
        self.assertTrue(any("FOR UPDATE" in q["sql"] for q in queries))
        self.assertEqual(self.snapshot(), before)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "unloaded")
        indexing.assert_called_once_with(str(self.event.pk))

    def test_conflicting_later_edit_refuses_undo_without_partial_restoration(self):
        self.merge()
        tile = models.TileModel.objects.get(pk=self.tiles[0].pk)
        tile.data[str(self.date_node.pk)] = "1920-01-01"
        models.TileModel.objects.filter(pk=tile.pk).update(data=tile.data)
        before_undo = self.snapshot()
        with patch.object(self.editor, "_refresh_resource_indexes") as indexing:
            with self.assertRaisesRegex(ValueError, "review later edits"):
                self.editor.reverse_load(str(self.event.pk))
        self.assertEqual(self.snapshot(), before_undo)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "indexed")
        self.assertIn("review later edits", self.event.error_message)
        indexing.assert_not_called()
