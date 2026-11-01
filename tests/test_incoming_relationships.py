"""A merge must not delete the target of an unbacked incoming reference."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests import test_resources_merge as fixtures


class IncomingRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.IndexLifecycleTests('runTest')
        self.h.setUp()
        self.h.prepare_merge()
        del self.h.merge._validate_incoming_references
        self.h.ns['RXR_NODE_FIELD'] = 'nodeid'
        self.references = [
            {'resourceId': 'base', 'resourceXresourceId': 'base-rx'},
            {'resourceId': 'source', 'resourceXresourceId': 'missing-rx'},
        ]
        self.tile = SimpleNamespace(tileid='payment-tile', resourceinstance_id='peer',
                                    data={'actor': self.references}, parenttile_id=None, parenttile=None)
        self.rows = [('base-rx', 'payment-tile', 'peer', 'base', 'actor')]
        self.links = Mock()
        self.links.values_list.side_effect = lambda *args, **kw: (
            ['payment-tile'] if kw.get('flat') else self.rows)
        self.h.ns['ResourceXResource'].objects.filter = lambda **kw: (
            self.links if self.h.ns['RXR_TO_FIELD'] + '__in' in kw else [])
        tiles = Mock()
        tiles.values_list.return_value = []
        tiles.iterator.side_effect = lambda **kw: iter([self.tile])
        self.h.ns['Tile'].objects.filter = lambda **kw: tiles if 'tileid__in' in kw else []
        self.h.ns['models'] = SimpleNamespace(TileModel=SimpleNamespace(
            objects=SimpleNamespace(filter=lambda **kw: tiles)))

    def test_reference_validation_does_not_construct_graph_aware_tiles(self):
        self.rows.append(('missing-rx', 'payment-tile', 'peer', 'source', 'actor'))
        before = copy.deepcopy(self.tile.data)
        self.h.ns['Tile'].objects.filter = Mock(side_effect=AssertionError(
            'Reference-only validation must not load a graph for each tile'))
        self.h.merge._validate_incoming_references('base', ['source'])
        self.assertEqual(self.tile.data, before)

    def test_execution_refuses_unbacked_source_reference_before_mutation(self):
        before = copy.deepcopy(self.tile.data)
        result = self.h.merge.run_load_task('user', 'test-load', 'base', ['source'])
        self.assertFalse(result['success'])
        self.assertIn('relationship', result['data']['message'])
        self.h.merge._process_merge_resource.assert_not_called()
        self.h.merge._resource_delete.assert_not_called()
        self.h.deleter.delete_index.assert_not_called()
        self.assertEqual(self.tile.data, before)

    def test_valid_rows_and_unrelated_dangling_references_are_allowed(self):
        self.rows.append(('missing-rx', 'payment-tile', 'peer', 'source', 'actor'))
        self.references.extend([None, {}, {'resourceId': ''},
                                {'resourceId': 'unrelated', 'resourceXresourceId': 'other-missing'}])
        before = copy.deepcopy(self.tile.data)
        result = self.h.merge.run_load_task('user', 'test-load', 'base', ['source'])
        self.assertTrue(result['success'])
        self.assertEqual(self.tile.data, before)

    def test_unbacked_reference_in_incoming_tiles_parent_is_also_refused(self):
        parent = SimpleNamespace(tileid='payment-parent', resourceinstance_id='peer',
                                 data={'actor': [self.references[1]]}, parenttile_id=None, parenttile=None)
        self.tile.data = {'actor': [self.references[0]]}
        self.tile.parenttile = parent
        self.tile.parenttile_id = parent.tileid
        before = copy.deepcopy(parent.data)
        result = self.h.merge.run_load_task('user', 'test-load', 'base', ['source'])
        self.assertFalse(result['success'])
        self.h.merge._process_merge_resource.assert_not_called()
        self.assertEqual(parent.data, before)

    def test_preview_returns_clear_refusal_before_building_merge(self):
        self.h.ns['Resource'].objects.get.return_value = SimpleNamespace(graph_id='graph', graph='graph')
        self.h.ns['Graph'] = Mock()
        self.h.merge.get_source_references_ids = Mock(return_value=[])
        self.h.merge._validate_single_cards = Mock()
        existing_filter = self.h.ns['Tile'].objects.filter
        self.h.ns['Tile'].objects.filter = lambda **kw: [self.tile] if 'resourceinstance' in kw else existing_filter(**kw)
        self.h.merge.calculate_which_added_which_removed = Mock(side_effect=AssertionError('Unsafe preview reached'))
        request = SimpleNamespace(POST={'resourceBase': 'base', 'mergeResources': 'source'},
                                  user=self.h.merge.user)
        before = copy.deepcopy(self.tile.data)
        result = self.h.merge.get_mergeable_nodegroups(request)
        self.assertEqual(result['data']['info'], 'No')
        self.assertIn('relationship', result['data']['info_message'])
        self.h.merge.calculate_which_added_which_removed.assert_not_called()
        self.assertEqual(self.tile.data, before)

    def test_mismatched_row_identity_is_refused_in_both_field_conventions(self):
        for fields in [('resourceinstanceidfrom', 'resourceinstanceidto', 'tileid', 'nodeid'),
                       ('from_resource', 'to_resource', 'tile', 'node')]:
            for index in range(1, 5):
                with self.subTest(fields=fields, wrong_field=index):
                    self.setUp()
                    self.h.ns.update(zip(['RXR_FROM_FIELD', 'RXR_TO_FIELD', 'RXR_TILE_FIELD', 'RXR_NODE_FIELD'], fields))
                    row = ['missing-rx', 'payment-tile', 'peer', 'source', 'actor']
                    row[index] = 'wrong'
                    self.rows.append(tuple(row))
                    result = self.h.merge.run_load_task('user', 'test-load', 'base', ['source'])
                    self.assertFalse(result['success'])
                    self.h.merge._process_merge_resource.assert_not_called()
