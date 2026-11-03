"""Single-occurrence cards must not discard nonblank source values."""
from types import SimpleNamespace
import unittest
from tests import test_resources_merge as fixtures


class SingleCardTests(unittest.TestCase):
    uri = fixtures.UriMergeTests.uri
    def setUp(self):
        fixtures.UriMergeTests.setUp(self)
        self.ns['NodeGroup'].objects.get=lambda **kw: SimpleNamespace(nodegroupid=kw['nodegroupid'],cardinality='1')
        self.merge.get_node=lambda key: SimpleNamespace(
            name='_label' if key=='label-node' else key,
            datatype='resource-instance-list' if key=='ref' else 'date' if key=='birth-date' else 'string')

    def test_group_and_place_labels_keep_base_and_unload_exactly(self):
        source_value = {'en': {'value': 'Source label', 'direction': 'ltr'}}
        for label_name, field in [('_label', 'label-node'), ('label', 'text')]:
            base_values = [{field: {'en': {'value': 'Base label', 'direction': 'ltr'}}},
                           {field: None}, {field: {}}, {}]
            for base_data in base_values:
                with self.subTest(label_name=label_name, base_data=base_data):
                    self.setUp()
                    self.merge.get_node = lambda key: SimpleNamespace(name=label_name, datatype='string')
                    base = self.add_tile('base-label', 'base', group='single', data=base_data)
                    source = self.add_tile('source-label', 'source', group='single', data={field: source_value})
                    before = self.snapshot()
                    self.merge._validate_single_cards([base], ['source'])
                    self.preview('source')
                    self.assertEqual(self.snapshot(), before)
                    self.execute('source')
                    self.assertEqual(self.tiles['base-label'].data, base_data)
                    self.assertNotIn('source-label', self.tiles)
                    self.merge.reverse_edit_log_entries('test-load')
                    self.assertEqual(self.snapshot(), before)
                    if base_data.get(field):
                        base.parenttile = SimpleNamespace(tileid='parent')
                        source.parenttile = SimpleNamespace(tileid='other-parent')
                        with self.assertRaisesRegex(ValueError, 'Conflicting nonblank'):
                            self.merge._single_card_data(base, source)

    def test_source_label_transfers_when_base_has_no_label_card(self):
        for owners in [('source',),('source','other')]:
            with self.subTest(owners=owners):
                self.setUp()
                for owner in owners:
                    self.add_tile(owner+'-label',owner,group='single',
                                  data={'label-node':{'en':{'value':owner,'direction':'ltr'}}})
                before=self.snapshot()
                self.merge._validate_single_cards([],list(owners))
                self.assertIn('source-label',self.preview(*owners))
                self.assertEqual(self.snapshot(),before)
                self.execute(*owners)
                self.assertEqual(self.tiles['source-label'].resourceinstance,'base')
                self.assertEqual(self.tiles['source-label'].data['label-node']['en']['value'],'source')
                self.assertNotIn('other-label',self.tiles)
                self.merge.reverse_edit_log_entries('test-load')
                self.assertEqual(self.snapshot(),before)

    def test_nested_label_and_birth_date_conflicts_are_not_exempt(self):
        for key in ['label-node','birth-date']:
            with self.subTest(key=key):
                self.setUp()
                for owner in ['base','source']:
                    root=self.add_tile(owner+'-root',owner,group='single',data={})
                    child=self.add_tile(owner+'-child',owner,group='nested-single',parent=root,
                                        data={key:'1900-01-01' if owner=='base' else '1901-01-01'})
                    child.nodegroup.cardinality='1'
                    self.tiles[child.tileid].nodegroup.cardinality='1'
                before=self.snapshot()
                with self.assertRaisesRegex(ValueError,'Conflicting nonblank'):
                    self.merge._validate_single_cards(self.filter_tiles(resourceinstance='base'),['source'])
                self.assertEqual(self.snapshot(),before)

    def test_blank_base_fills_from_source_and_replays_exactly(self):
        self.add_tile('base-tile','base',group='single',data={'text':None})
        self.add_tile('source-tile','source',group='single',data={'text':{'en':{'value':'Source','direction':'ltr'}}})
        before=self.snapshot()
        self.execute('source')
        self.assertEqual(self.tiles['base-tile'].data['text']['en']['value'],'Source')
        self.merge.reverse_edit_log_entries('test-load')
        self.assertEqual(self.snapshot(),before)

    def test_source_reference_moves_owner_and_tile_and_replays(self):
        self.add_tile('base-tile','base',group='single',data={'ref':None})
        self.add_tile('source-tile','source',group='single',data={'ref':[{'resourceId':'peer','resourceXresourceId':'rx'}]})
        self.relations['rx']=SimpleNamespace(resourcexid='rx',resourceinstanceidfrom='source',tileid='source-tile')
        before=self.snapshot()
        self.execute('source')
        self.assertEqual(self.tiles['base-tile'].data['ref'][0]['resourceId'],'peer')
        self.assertEqual(self.relations['rx'].resourceinstanceidfrom,'base')
        self.assertEqual(self.relations['rx'].tileid.tileid,'base-tile')
        self.merge.reverse_edit_log_entries('test-load')
        self.assertEqual(self.snapshot(),before)

    def test_conflicting_scalar_stops_before_any_write(self):
        base=self.add_tile('base-tile','base',group='single',data={'text':'Base'})
        source=self.add_tile('source-tile','source',group='single',data={'text':'Source'})
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting nonblank'):
            self.merge._validate_single_cards([base], ['source'])
        self.assertEqual(self.snapshot(),before)
        with self.assertRaises(ValueError):
            self.merge._process_merge_tile('source',source,'base',[base])
        self.assertEqual(self.snapshot(),before)

    def test_preflight_detects_conflict_between_sources_with_empty_base(self):
        self.add_tile('first','source',group='single',data={'text':'First'})
        self.add_tile('second','other',group='single',data={'text':'Second'})
        before=self.snapshot()
        with self.assertRaises(ValueError):self.merge._validate_single_cards([],['source','other'])
        self.assertEqual(self.snapshot(),before)
        self.merge._validate_single_cards([],['source','other'],['second'])

    def test_lists_union_and_duplicate_reference_keeps_retained_identity(self):
        left={'resourceId':'peer','resourceXresourceId':'base-rx'}
        right={'resourceId':'peer','resourceXresourceId':'source-rx'}
        self.assertEqual(self.merge._single_card_value([left],[right]),[left])
        left['ontologyProperty']=''
        right['ontologyProperty']='filled'
        self.assertEqual(self.merge._single_card_value([left],[right])[0]['ontologyProperty'],'filled')
        self.assertEqual(self.merge._single_card_value(['a'],['b']),['a','b'])
        self.assertEqual(self.merge._single_card_value({'en':{'value':None}}, {'en':{'value':'Filled'}}),{'en':{'value':'Filled'}})

    def test_nested_blank_fill_moves_into_existing_child_and_restores(self):
        base=self.add_tile('base-root','base',group='single',data={})
        source=self.add_tile('source-root','source',group='single',data={})
        self.add_tile('base-child','base',group='single-child',parent=base,data={'text':None})
        self.add_tile('source-child','source',group='single-child',parent=source,data={'text':'Filled'})
        before=self.snapshot()
        self.execute('source')
        self.assertEqual(self.tiles['base-child'].data['text'],'Filled')
        self.assertNotIn('source-child',self.tiles)
        self.merge.reverse_edit_log_entries('test-load')
        self.assertEqual(self.snapshot(),before)

    def test_scalar_reference_child_transfer_logs_owner_for_unload(self):
        self.add_tile('base-root','base',group='single',data={})
        source=self.add_tile('source-root','source',group='single',data={})
        self.add_tile('child','source',group='single-child',parent=source,data={'ref':{'resourceId':'peer','resourceXresourceId':'rx'}})
        self.relations['rx']=SimpleNamespace(resourcexid='rx',resourceinstanceidfrom='source',tileid='child')
        before=self.snapshot()
        self.execute('source')
        self.assertEqual(self.tiles['child'].parenttile.tileid,'base-root')
        self.assertEqual(self.tiles['child'].resourceinstance,'base')
        self.assertEqual(self.relations['rx'].resourceinstanceidfrom,'base')
        self.merge.reverse_edit_log_entries('test-load')
        self.assertEqual(self.snapshot(),before)

for name in ['get_tile','filter_tiles','filter_relations','save','delete_tile','deserialize','snapshot','cleanup','execute','preview']:
    setattr(SingleCardTests,name,getattr(fixtures.UriMergeTests,name))

def single_tile(self, id, owner, **kwargs):
    tile=fixtures.UriMergeTests.add_tile(self,id,owner,**kwargs)
    if kwargs.get('group')=='single':
        tile.nodegroup.cardinality='1'
        self.tiles[id].nodegroup.cardinality='1'
    return tile
SingleCardTests.add_tile=single_tile
