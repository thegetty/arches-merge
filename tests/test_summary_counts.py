"""Summary-only reads and operation-scoped metadata reuse."""
from types import SimpleNamespace as Obj
from unittest import TestCase
from unittest.mock import Mock
from tests.test_resources_merge import load_merge_methods, Query


class SummaryMetadataTests(TestCase):
    def setUp(self):
        self.ns = load_merge_methods()
        self.editor = self.ns['Resourcesmerge'].__new__(self.ns['Resourcesmerge'])
        self.card = Obj(name='First label')
        self.other_card = Obj(name='Second label')
        self.groups = Mock()
        self.groups.get.return_value = Obj(nodegroupid='ng')
        self.ns['models'] = Obj(NodeGroup=Obj(objects=self.groups))
        self.ns['Node'] = Obj(objects=Obj(filter=Mock(return_value=['empty', 'with-card'])))
        self.ns['CardXNodeXWidget'] = Obj(objects=Obj(filter=Mock(side_effect=lambda **kw: Query([] if kw['node']=='empty' else [Obj(card=self.card), Obj(card=self.other_card)]))))

    def test_both_name_helpers_share_resolution_and_keep_first_widget_order(self):
        tile = Obj(nodegroup_id='ng')
        self.assertEqual(self.editor.get_name_card(tile), ('First label', 'ng'))
        self.assertEqual(self.editor.get_card_name_by_nodegroupid('ng'), 'First label')
        self.assertEqual(self.editor.get_name_card(tile), ('First label', 'ng'))
        self.groups.get.assert_called_once()

    def test_missing_widget_uses_nodegroup_fallback(self):
        self.ns['CardXNodeXWidget'].objects.filter.side_effect = lambda **kw: Query([])
        self.assertEqual(self.editor.get_name_card(Obj(nodegroup_id='ng')), ('ng', 'ng'))
        self.assertEqual(self.editor.get_card_name_by_nodegroupid('ng'), 'ng')
        self.groups.get.assert_called_once()

    def test_each_preview_or_merge_sees_updated_metadata(self):
        for operation in ['preview', 'merge']:
            with self.subTest(operation=operation):
                self.setUp()
                self.editor.get_card_name_by_nodegroupid('ng')
                self.card.name = 'Edited label'
                if operation == 'preview':
                    self.ns['Resource'] = Obj(objects=Obj(get=Mock(side_effect=RuntimeError('before resource access'))))
                    request = Obj(POST={'resourceBase': 'base', 'mergeResources': 'source'}, user=Obj(username='test'))
                    with self.assertRaisesRegex(RuntimeError, 'before resource access'):
                        self.editor.get_mergeable_nodegroups(request)
                else:
                    self.editor.user = None
                    self.ns['User'] = Obj(objects=Obj(get=Mock(side_effect=RuntimeError('before user access'))))
                    with self.assertRaisesRegex(RuntimeError, 'before user access'):
                        self.editor.run_load_task('user', 'load', 'base', ['source'])
                self.assertEqual(self.editor.get_card_name_by_nodegroupid('ng'), 'Edited label')

    def test_raw_count_rows_keep_reference_sets_and_excluded_tile_ownership(self):
        rows = [Obj(tileid='a',nodegroup_id='normal'),Obj(tileid='b',nodegroup_id='normal'),Obj(tileid='c',nodegroup_id='ref')]
        raw = Mock(return_value=rows)
        self.ns['models'].TileModel = Obj(objects=Obj(filter=raw))
        self.ns['Tile'] = Obj(objects=Obj(filter=Mock(side_effect=AssertionError('Graph construction is unnecessary'))))
        self.editor.get_name_card = lambda tile: (tile.nodegroup_id, tile.nodegroup_id)
        counts, reference = self.editor.calculate_number_of_tiles_per_nodegroup_baseresource('base',{},'ref',['x','x','y'])
        self.assertEqual(counts, {'normal':{'base_pre':2},'ref':{'base_pre':2}})
        self.assertEqual(reference, 'ref')
        added, excluded, reference = self.editor.build_dict_merge_resources_tiles_per_nodegroup('source',{}, {},'ref',['x','x'],['y'],['b','c'])
        self.assertEqual(added, {'normal':{'source':['a']}})
        self.assertEqual(excluded, {'normal':{'source':['b']},'ref':{'source':{'y'}}})
        self.assertEqual(reference,'ref')
