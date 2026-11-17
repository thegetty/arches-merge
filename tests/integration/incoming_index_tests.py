"""Real PostgreSQL concurrent DDL; run only through Django's isolated test DB."""
from importlib import import_module
import json
from uuid import uuid4

from django.db import connection, OperationalError, transaction
from django.test import TransactionTestCase

from arches.app.models import models


migration = import_module(
    "arches_resource_merge.migrations.0003_add_incoming_reference_index"
)


class IncomingIndexMigrationTests(TransactionTestCase):
    def setUp(self):
        self.assertFalse(connection.in_atomic_block)
        self.assertFalse(migration.Migration.atomic)
        with connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS public.{migration.INDEX_NAME}")
        self.addCleanup(self.restore_index)

    def restore_index(self):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS public.{migration.INDEX_NAME}")
        self.forward()

    def forward(self):
        with connection.schema_editor(atomic=False) as editor:
            migration.forwards(None, editor)

    def index_state(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.oid, i.indisvalid, pg_get_indexdef(c.oid) FROM pg_class c "
                "JOIN pg_index i ON i.indexrelid = c.oid WHERE c.oid = to_regclass(%s)",
                ["public." + migration.INDEX_NAME],
            )
            return cursor.fetchone()

    def test_create_reuse_reverse_and_indexed_lookup_preserve_tiles(self):
        graph = models.GraphModel.objects.create(name="Index test", isresource=True)
        group = models.NodeGroup.objects.create(cardinality="n")
        owner = models.ResourceInstance.objects.create(graph=graph)
        target, other, node = map(str, (uuid4(), uuid4(), uuid4()))
        values = [
            {node: {"resourceId": target}},
            {node: [{"resourceId": other}, {"resourceId": target}]},
            {node: [{"resourceId": other}]},
            {node: target},  # An unrelated scalar UUID must not match.
            {},
        ]
        tiles = [models.TileModel.objects.create(resourceinstance=owner,
                 nodegroup=group, data=data) for data in values]
        before = list(models.TileModel.objects.order_by("pk").values())
        self.forward()
        state = self.index_state()
        self.assertTrue(state[1])
        self.forward()
        self.assertEqual(self.index_state(), state, "Existing index was rebuilt")
        with transaction.atomic(), connection.cursor() as cursor:
            # A tiny fixture otherwise favors a sequential scan; check eligibility.
            cursor.execute("SET LOCAL enable_seqscan = off")
            query = ("SELECT tileid FROM public.tiles WHERE "
                     "jsonb_path_query_array(tiledata, '$.*.resourceId') ?| %s::text[]")
            cursor.execute(query, [[target]])
            self.assertEqual({row[0] for row in cursor.fetchall()}, {t.pk for t in tiles[:2]})
            cursor.execute("EXPLAIN (FORMAT JSON) " + query, [[target]])
            self.assertIn(migration.INDEX_NAME, json.dumps(cursor.fetchone()[0]))
        with connection.schema_editor(atomic=False) as editor:
            migration.backwards(None, editor)
            migration.backwards(None, editor)
        self.assertIsNone(self.index_state())
        self.assertEqual(list(models.TileModel.objects.order_by("pk").values()), before)

    def test_incompatible_existing_indexes_are_refused_without_replacement(self):
        expression = "jsonb_path_query_array(tiledata, '$.*.resourceId')"
        definitions = [
            "ON public.resource_instances (resourceinstanceid)",
            "ON public.tiles USING gin (tiledata)",
            f"ON public.tiles USING gin ({expression} jsonb_path_ops)",
            f"ON public.tiles USING gin ({expression}) WHERE tiledata IS NOT NULL",
        ]
        for definition in definitions:
            with self.subTest(definition=definition):
                with connection.cursor() as cursor:
                    cursor.execute(f"CREATE INDEX {migration.INDEX_NAME} {definition}")
                before = self.index_state()
                with self.assertRaisesRegex(RuntimeError, "incompatible definition"):
                    self.forward()
                self.assertEqual(self.index_state(), before)
                with connection.schema_editor(atomic=False) as editor:
                    with self.assertRaisesRegex(RuntimeError, "incompatible definition"):
                        migration.backwards(None, editor)
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP INDEX public.{migration.INDEX_NAME}")

    def test_interrupted_concurrent_index_is_refused_without_replacement(self):
        blocker = connection.Database.connect(**connection.get_connection_params())
        try:
            with blocker.cursor() as cursor:
                cursor.execute("LOCK TABLE public.tiles IN ROW EXCLUSIVE MODE")
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '100ms'")
                try:
                    with self.assertRaises(OperationalError):
                        cursor.execute(migration.CREATE_INDEX)
                finally:
                    cursor.execute("RESET lock_timeout")
        finally:
            blocker.rollback()
            blocker.close()
        before = self.index_state()
        self.assertIsNotNone(before)
        self.assertFalse(before[1])
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.forward()
        self.assertEqual(self.index_state(), before)
